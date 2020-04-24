# -*- coding: utf-8 -*-
# Copyright 2007-2020 The HyperSpy developers
#
# This file is part of  HyperSpy.
#
#  HyperSpy is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
#  HyperSpy is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with  HyperSpy.  If not, see <http://www.gnu.org/licenses/>.


import logging
import types
import warnings

import dask.array as da
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.ticker import FuncFormatter, MaxNLocator

import hyperspy.misc.io.tools as io_tools
from hyperspy.exceptions import VisibleDeprecationWarning
from hyperspy.learn.mlpca import mlpca
from hyperspy.learn.ornmf import ornmf
from hyperspy.learn.orthomax import orthomax
from hyperspy.learn.rpca import orpca, rpca_godec
from hyperspy.learn.svd_pca import svd_pca
from hyperspy.learn.whitening import whiten_data
from hyperspy.misc.machine_learning import import_sklearn
from hyperspy.misc.utils import ordinal, stack

try:
    import mdp

    mdp_installed = True
except ImportError:
    mdp_installed = False


_logger = logging.getLogger(__name__)


if import_sklearn.sklearn_installed:
    decomposition_algorithms = {
        "sklearn_pca": import_sklearn.sklearn.decomposition.PCA,
        "nmf": import_sklearn.sklearn.decomposition.NMF,
        "sparse_pca": import_sklearn.sklearn.decomposition.SparsePCA,
        "mini_batch_sparse_pca": import_sklearn.sklearn.decomposition.MiniBatchSparsePCA,
        "sklearn_fastica": import_sklearn.sklearn.decomposition.FastICA,
    }


def _get_derivative(signal, diff_axes, diff_order):
    """Calculate the derivative of a signal."""
    if signal.axes_manager.signal_dimension == 1:
        signal = signal.diff(order=diff_order, axis=-1)
    else:
        # n-d signal case.
        # Compute the differences for each signal axis, unfold the
        # signal axes and stack the differences over the signal
        # axis.
        if diff_axes is None:
            diff_axes = signal.axes_manager.signal_axes
            iaxes = [axis.index_in_axes_manager for axis in diff_axes]
        else:
            iaxes = diff_axes
        diffs = [signal.derivative(order=diff_order, axis=i) for i in iaxes]
        for signal in diffs:
            signal.unfold()
        signal = stack(diffs, axis=-1)
        del diffs
    return signal


def _normalize_components(target, other, function=np.sum):
    """Normalize components according to a function."""
    coeff = function(target, axis=0)
    target /= coeff
    other *= coeff


class MVA:
    """Multivariate analysis capabilities for the Signal1D class."""

    def __init__(self):
        if not hasattr(self, "learning_results"):
            self.learning_results = LearningResults()

    def decomposition(
        self,
        normalize_poissonian_noise=False,
        algorithm="svd",
        output_dimension=None,
        centre=None,
        auto_transpose=True,
        navigation_mask=None,
        signal_mask=None,
        var_array=None,
        var_func=None,
        reproject=None,
        return_info=False,
        print_info=True,
        svd_solver="auto",
        copy=True,
        **kwargs
    ):
        """Decomposition with a choice of algorithms.

        The results are stored in `self.learning_results`.

        Read more in the :ref:`User Guide <mva.decomposition>`.

        Parameters
        ----------
        normalize_poissonian_noise : bool, default False
            If True, scale the signal to normalize Poissonian noise using
            the approach described in [Keenan2004]_.
        algorithm : {'svd', 'mlpca', 'sklearn_pca', 'nmf', 'sparse_pca', 'mini_batch_sparse_pca', 'rpca', 'orpca', 'ornmf', custom object}, default 'svd'
            The decomposition algorithm to use. The default is 'svd'. If
            algorithm is an object, it must implement a `fit_transform()`
            method or `fit()` and `transform()` methods, in the same manner
            as a scikit-learn estimator.
        output_dimension : None or int
            Number of components to keep/calculate.
            Default is None, i.e. `min(data.shape)`.
        centre : {None, 'features', 'samples'}, default None
            * If None, the data is not centered.
            * If 'features', the data is centered along the features axis.
              Only used by the 'svd' algorithm.
            * If 'samples', the data is centered along the samples axis.
              Only used by the 'svd' algorithm.
        auto_transpose : bool, default True
            If True, automatically transposes the data to boost performance.
            Only used by the 'svd' algorithm.
        navigation_mask : boolean numpy array
            The navigation locations marked as True are not used in the
            decompostion.
        signal_mask : boolean numpy array
            The signal locations marked as True are not used in the
            decomposition.
        var_array : numpy array
            Array of variance for the maximum likelihood PCA algorithm.
            Only used by the 'mlpca' algorithm.
        var_func : None or function or numpy array, default None
            * If None, ignored
            * If function, applies the function to the data to obtain var_array.
              Only used by the 'mlpca' algorithm.
            * If numpy array, creates var_array by applying a polynomial function
              defined by the array of coefficients to the data. Only used by
              the 'mlpca' algorithm.
        reproject : {None, 'signal', 'navigation', 'both'}, default None
            If not None, the results of the decomposition will be projected in
            the selected masked area.
        return_info: bool, default False
            The result of the decomposition is stored internally. However,
            some algorithms generate some extra information that is not
            stored. If True, return any extra information if available.
            In the case of sklearn.decomposition objects, this includes the
            sklearn Estimator object.
        print_info : bool, default True
            If True, print information about the decomposition being performed.
            In the case of sklearn.decomposition objects, this includes the
            values of all arguments of the chosen sklearn algorithm.
        svd_solver : {'auto', 'full', 'arpack', 'randomized'}, default 'auto'
            If auto:
                The solver is selected by a default policy based on `data.shape` and
                `output_dimension`: if the input data is larger than 500x500 and the
                number of components to extract is lower than 80% of the smallest
                dimension of the data, then the more efficient 'randomized'
                method is enabled. Otherwise the exact full SVD is computed and
                optionally truncated afterwards.
            If full:
                run exact SVD, calling the standard LAPACK solver via
                :py:func:`scipy.linalg.svd`, and select the components by postprocessing
            If arpack:
                use truncated SVD, calling ARPACK solver via
                :py:func:`scipy.sparse.linalg.svds`. It requires strictly
                `0 < output_dimension < min(data.shape)`
            If randomized:
                use truncated SVD, calling :py:func:`sklearn.utils.extmath.randomized_svd`
                to estimate a limited number of components
        copy : bool, default True
            * If True, stores a copy of the data before any pre-treatments
              such as normalization in ``s._data_before_treatments``. The original
              data can then be restored by calling ``s.undo_treatments()``.
            * If False, no copy is made. This can be beneficial for memory
              usage, but care must be taken since data will be overwritten.

        Returns
        -------
        return_info : tuple(numpy array, numpy array) or sklearn.Estimator or None
            * If True and 'algorithm' in ['rpca', 'orpca', 'ornmf'], returns
              the low-rank (X) and sparse (E) matrices from robust PCA/NMF.
            * If True and 'algorithm' is an sklearn Estimator, returns the
              Estimator object.
            * Otherwise, returns None

        References
        ----------
        .. [Keenan2004] M. Keenan and P. Kotula, "Accounting for Poisson noise
            in the multivariate analysis of ToF-SIMS spectrum images", Surf.
            Interface Anal 36(3) (2004): 203-212.

        See Also
        --------
        :py:meth:`~.signal.MVATools.plot_decomposition_factors`,
        :py:meth:`~.signal.MVATools.plot_decomposition_loadings`,
        :py:meth:`~.signal.MVATools.plot_decomposition_results`,
        :py:meth:`~.learn.mva.MVA.plot_explained_variance_ratio`,

        """
        # Check data is suitable for decomposition
        if self.data.dtype.char not in np.typecodes["AllFloat"]:
            raise TypeError(
                (
                    "To perform a decomposition the data must be of the "
                    "float or complex type, but the current type is '{}'. "
                    "To fix this issue, you can change the type using the "
                    "change_dtype method (e.g. s.change_dtype('float64')) "
                    "and then repeat the decomposition.\n"
                    "No decomposition was performed."
                ).format(self.data.dtype)
            )

        if self.axes_manager.navigation_size < 2:
            raise AttributeError(
                "It is not possible to decompose a dataset with navigation_size < 2"
            )

        # Check for deprecated algorithm arguments
        algorithms_deprecated = {
            "fast_svd": "svd",
            "fast_mlpca": "mlpca",
            "RPCA_GoDec": "rpca",
            "ORPCA": "orpca",
            "ORNMF": "ornmf",
        }
        new_algo = algorithms_deprecated.get(algorithm, None)
        if new_algo:
            if "fast" in algorithm:
                warnings.warn(
                    "The algorithm name `{}` has been deprecated and will be "
                    "removed in HyperSpy 2.0. Please use `{}` along with the "
                    "argument `svd_solver='randomized'` instead.".format(
                        algorithm, new_algo
                    ),
                    VisibleDeprecationWarning,
                )
                svd_solver = "randomized"
            else:
                warnings.warn(
                    "The algorithm name `{}` has been deprecated and will be "
                    "removed in HyperSpy 2.0. Please use `{}` instead.".format(
                        algorithm, new_algo
                    ),
                    VisibleDeprecationWarning,
                )

            # Update algorithm name
            algorithm = new_algo

        # Check algorithms requiring output_dimension
        algorithms_require_dimension = [
            "mlpca",
            "rpca",
            "orpca",
            "ornmf",
        ]
        if algorithm in algorithms_require_dimension and output_dimension is None:
            raise ValueError(
                "`output_dimension` must be specified for '{}'".format(algorithm)
            )

        # Check sklearn-like algorithms
        is_sklearn_like = False
        algorithms_sklearn = [
            "sklearn_pca",
            "nmf",
            "sparse_pca",
            "mini_batch_sparse_pca",
        ]
        if algorithm in algorithms_sklearn:
            if not import_sklearn.sklearn_installed:
                raise ImportError(
                    "algorithm='{}' requires scikit-learn".format(algorithm)
                )

            # Initialize the sklearn estimator
            is_sklearn_like = True
            estim = decomposition_algorithms[algorithm](
                n_components=output_dimension, **kwargs
            )

        elif hasattr(algorithm, "fit_transform") or (
            hasattr(algorithm, "fit") and hasattr(algorithm, "transform")
        ):
            # Check properties of algorithm against typical sklearn objects
            # If algorithm is an object that implements the methods fit(),
            # transform() and fit_transform(), then we can use it like an
            # sklearn estimator. This also allows us to, for example, use
            # Pipeline and GridSearchCV objects.
            is_sklearn_like = True
            estim = algorithm

        # MLPCA is designed to handle count data & Poisson noise
        if algorithm == "mlpca" and normalize_poissonian_noise:
            warnings.warn(
                "It does not make sense to normalize Poisson noise with "
                "the maximum-likelihood MLPCA algorithm. Therefore, "
                "`normalize_poissonian_noise` is set to False.",
                UserWarning,
            )
            normalize_poissonian_noise = False

        # Check for deprecated polyfit
        polyfit = kwargs.get("polyfit", False)
        if polyfit:
            warnings.warn(
                "The `polyfit` argument has been deprecated and will be "
                "removed in HyperSpy 2.0. Please use `var_func` instead.",
                VisibleDeprecationWarning,
            )
            var_func = polyfit

        # Initialize return_info and print_info
        to_return = None
        to_print = [
            "Decomposition info:",
            "  normalize_poissonian_noise={}".format(normalize_poissonian_noise),
            "  algorithm={}".format(algorithm),
            "  output_dimension={}".format(output_dimension),
            "  centre={}".format(centre),
        ]

        # Backup the original data (on by default to
        # mimic previous behaviour)
        if copy:
            self._data_before_treatments = self.data.copy()

        # set the output target (peak results or not?)
        target = LearningResults()

        # Apply pre-treatments
        # Transform the data in a line spectrum
        self._unfolded4decomposition = self.unfold()
        try:
            _logger.info("Performing decomposition analysis")

            if hasattr(navigation_mask, "ravel"):
                navigation_mask = navigation_mask.ravel()

            if hasattr(signal_mask, "ravel"):
                signal_mask = signal_mask.ravel()

            # Normalize the poissonian noise
            # TODO this function can change the masks and
            # this can cause problems when reprojecting
            if normalize_poissonian_noise:
                if centre is not None:
                    raise ValueError(
                        "normalize_poissonian_noise=True is only compatible "
                        "with centre=None, not centre={}.".format(centre)
                    )

                self.normalize_poissonian_noise(
                    navigation_mask=navigation_mask, signal_mask=signal_mask,
                )

            # The rest of the code assumes that the first data axis
            # is the navigation axis. We transpose the data if that
            # is not the case.
            if self.axes_manager[0].index_in_array == 0:
                dc = self.data
            else:
                dc = self.data.T

            # Transform the None masks in slices to get the right behaviour
            if navigation_mask is None:
                navigation_mask = slice(None)
            else:
                navigation_mask = ~navigation_mask
            if signal_mask is None:
                signal_mask = slice(None)
            else:
                signal_mask = ~signal_mask

            # WARNING: signal_mask and navigation_mask values are now their
            # negaties i.e. True -> False and viceversa. However, the
            # stored value (at the end of the method) coincides with the
            # input masks

            data_ = dc[:, signal_mask][navigation_mask, :]

            # Reset the explained_variance which is not set by all the
            # algorithms
            explained_variance = None
            explained_variance_ratio = None
            number_significant_components = None
            mean = None

            if algorithm == "svd":
                factors, loadings, explained_variance, mean = svd_pca(
                    data_,
                    svd_solver=svd_solver,
                    output_dimension=output_dimension,
                    centre=centre,
                    auto_transpose=auto_transpose,
                    **kwargs,
                )

            elif algorithm == "mlpca":
                if var_array is not None and var_func is not None:
                    raise ValueError(
                        "`var_func` and `var_array` cannot both be defined. "
                        "Please define just one of them."
                    )
                elif var_array is None and var_func is None:
                    _logger.info(
                        "No variance array provided. Assuming Poisson-distributed data"
                    )
                    var_array = data_
                elif var_array is not None:
                    if var_array.shape != data_.shape:
                        raise ValueError(
                            "`var_array` must have the same shape as input data"
                        )
                elif var_func is not None:
                    if callable(var_func):
                        var_array = var_func(data_)
                    elif isinstance(var_func, (np.ndarray, list)):
                        var_array = np.polyval(var_func, data_)
                    else:
                        raise ValueError(
                            "`var_func` must be either a function or an array "
                            "defining the coefficients of a polynomial"
                        )

                U, S, V, Sobj = mlpca(
                    data_, var_array, output_dimension, svd_solver=svd_solver, **kwargs,
                )

                loadings = U * S
                factors = V
                explained_variance = S ** 2 / len(factors)

            elif algorithm == "rpca":
                X, E, U, S, V = rpca_godec(data_, rank=output_dimension, **kwargs)

                loadings = U * S
                factors = V
                explained_variance = S ** 2 / len(factors)

                if return_info:
                    to_return = (X, E)

            elif algorithm == "orpca":
                if return_info:
                    X, E, U, S, V = orpca(
                        data_, rank=output_dimension, store_error=True, **kwargs
                    )

                    loadings = U * S
                    factors = V
                    explained_variance = S ** 2 / len(factors)

                    to_return = (X, E)

                else:
                    L, R = orpca(data_, rank=output_dimension, **kwargs)

                    loadings = L
                    factors = R.T

            elif algorithm == "ornmf":
                if return_info:
                    X, E, W, H = ornmf(
                        data_, rank=output_dimension, store_error=True, **kwargs,
                    )
                    to_return = (X, E)
                else:
                    W, H = ornmf(data_, rank=output_dimension, **kwargs)

                loadings = W
                factors = H.T

            elif is_sklearn_like:
                if hasattr(estim, "fit_transform"):
                    loadings = estim.fit_transform(data_)
                elif hasattr(algorithm, "fit") and hasattr(algorithm, "transform"):
                    algorithm.fit(data_)
                    loadings = algorithm.transform(data_)

                if hasattr(estim, "steps"):
                    last_step = estim[-1]
                elif hasattr(estim, "best_estimator_"):
                    last_step = estim.best_estimator_
                else:
                    last_step = estim

                factors = last_step.components_.T

                if hasattr(last_step, "explained_variance_"):
                    explained_variance = last_step.explained_variance_

                if hasattr(last_step, "mean_"):
                    mean = last_step.mean_
                    centre = "samples"

                to_print.extend(["scikit-learn estimator:", estim])
                if return_info:
                    to_return = estim

            else:
                raise ValueError("'algorithm' not recognised")

            # We must calculate the ratio here because otherwise the sum
            # information can be lost if the user subsequently calls
            # crop_decomposition_dimension()
            if explained_variance is not None and explained_variance_ratio is None:
                explained_variance_ratio = explained_variance / explained_variance.sum()
                number_significant_components = (
                    self.estimate_elbow_position(explained_variance_ratio) + 1
                )

            # Store the results in learning_results
            target.factors = factors
            target.loadings = loadings
            target.explained_variance = explained_variance
            target.explained_variance_ratio = explained_variance_ratio
            target.number_significant_components = number_significant_components
            target.decomposition_algorithm = algorithm
            target.poissonian_noise_normalized = normalize_poissonian_noise
            target.output_dimension = output_dimension
            target.unfolded = self._unfolded4decomposition
            target.centre = centre
            target.mean = mean

            if output_dimension and factors.shape[1] != output_dimension:
                target.crop_decomposition_dimension(output_dimension)

            # Delete the unmixing information, as it will refer to a
            # previous decomposition
            target.unmixing_matrix = None
            target.bss_algorithm = None

            if self._unfolded4decomposition:
                folding = self.metadata._HyperSpy.Folding
                target.original_shape = folding.original_shape

            # Reproject
            if mean is None:
                mean = 0

            if reproject in ("navigation", "both"):
                if not is_sklearn_like:
                    loadings_ = (dc[:, signal_mask] - mean) @ factors
                else:
                    loadings_ = estim.transform(dc[:, signal_mask])
                target.loadings = loadings_

            if reproject in ("signal", "both"):
                if not is_sklearn_like:
                    factors = (
                        np.linalg.pinv(loadings) @ (dc[navigation_mask, :] - mean)
                    ).T
                    target.factors = factors
                else:
                    warnings.warn(
                        "Reprojecting the signal is not yet "
                        "supported for algorithm='{}'".format(algorithm),
                        UserWarning,
                    )
                    if reproject == "both":
                        reproject = "signal"
                    else:
                        reproject = None

            # Rescale the results if the noise was normalized
            if normalize_poissonian_noise:
                target.factors[:] *= self._root_bH.T
                target.loadings[:] *= self._root_aG

            # Set the pixels that were not processed to nan
            if not isinstance(signal_mask, slice):
                # Store the (inverted, as inputed) signal mask
                target.signal_mask = ~signal_mask.reshape(
                    self.axes_manager._signal_shape_in_array
                )
                if reproject not in ("both", "signal"):
                    factors = np.zeros((dc.shape[-1], target.factors.shape[1]))
                    factors[signal_mask, :] = target.factors
                    factors[~signal_mask, :] = np.nan
                    target.factors = factors

            if not isinstance(navigation_mask, slice):
                # Store the (inverted, as inputed) navigation mask
                target.navigation_mask = ~navigation_mask.reshape(
                    self.axes_manager._navigation_shape_in_array
                )
                if reproject not in ("both", "navigation"):
                    loadings = np.zeros((dc.shape[0], target.loadings.shape[1]))
                    loadings[navigation_mask, :] = target.loadings
                    loadings[~navigation_mask, :] = np.nan
                    target.loadings = loadings

        finally:
            if self._unfolded4decomposition:
                self.fold()
                self._unfolded4decomposition = False
            self.learning_results.__dict__.update(target.__dict__)

            # Undo any pre-treatments by restoring the copied data
            if copy:
                self.undo_treatments()

        # Print details about the decomposition we just performed
        if print_info:
            print("\n".join([str(pr) for pr in to_print]))

        return to_return

    def blind_source_separation(
        self,
        number_of_components=None,
        algorithm="sklearn_fastica",
        diff_order=1,
        diff_axes=None,
        factors=None,
        comp_list=None,
        mask=None,
        on_loadings=False,
        reverse_component_criterion="factors",
        whiten_method="pca",
        return_info=False,
        print_info=True,
        **kwargs
    ):
        """Blind source separation (BSS) on the result on the decomposition.

        Available algorithms:  orthomax, FastICA, JADE, CuBICA, and TDSEP.

        For lazy signal, the factors or loadings are computed to perfom the
        BSS.

        Read more in the :ref:`User Guide <mva.blind_source_separation>`.

        Parameters
        ----------
        number_of_components : int
            number of principal components to pass to the BSS algorithm
        algorithm : {"sklearn_fastica", "orthomax", "FastICA", "JADE", "CuBICA", "TDSEP"}
            BSS algorithms available. If "sklearn_fastica", uses the scikit-learn
            library to perform FastICA, otherwise use the Modular toolkit for Data
            Processing (MDP) is used. Default is "sklearn_fastica".
        diff_order : int
            Sometimes it is convenient to perform the BSS on the derivative of
            the signal. If `diff_order` is 0, the signal is not differentiated.
        diff_axes : None or list of ints or strings
            If None, when `diff_order` is greater than 1 and `signal_dimension`
            (`navigation_dimension`) when `on_loadings` is False (True) is
            greater than 1, the differences are calculated across all
            signal (navigation) axes. Otherwise the axes can be specified in
            a list.
        factors : Signal or numpy array
            Factors to decompose. If None, the BSS is performed on the
            factors of a previous decomposition. If a Signal instance the
            navigation dimension must be 1 and the size greater than 1.
        comp_list : boolean numpy array
            choose the components to use by the boolean list. It permits
            to choose non contiguous components.
        mask : :py:class:`~hyperspy.signal.BaseSignal` (or subclass)
            If not None, the signal locations marked as True are masked. The
            mask shape must be equal to the signal shape
            (navigation shape) when `on_loadings` is False (True).
        on_loadings : bool, default False
            If True, perform the BSS on the loadings of a previous
            decomposition, otherwise, perform the BSS on the factors.
        reverse_component_criterion : {'factors', 'loadings'}
            Use either the factors or the loadings to determine if the
            component needs to be reversed. Default is 'factors'.
        whiten_method : {"pca", "zca"}
            How to whiten the data prior to blind source separation.
            The default is PCA whitening.
        return_info: bool, default False
            The result of the decomposition is stored internally. However,
            some algorithms generate some extra information that is not
            stored. If True, return any extra information if available.
            In the case of sklearn.decomposition objects, this includes the
            sklearn Estimator object.
        print_info : bool, default True
            If True, print information about the decomposition being performed.
            In the case of sklearn.decomposition objects, this includes the
            values of all arguments of the chosen sklearn algorithm.
        **kwargs : extra keyword arguments
            Any keyword arguments are passed to the BSS algorithm.

        Returns
        -------
        return_info : sklearn.Estimator or None
            * If True and 'algorithm' = 'sklearn_fastica', returns the
              sklearn Estimator object.
            * Otherwise, returns None

        Notes
        -----
        See the FastICA documentation, with more arguments that can be passed
        as kwargs :py:class:`sklearn.decomposition.FastICA`

        See Also
        --------
        :py:meth:`~.signal.MVATools.plot_bss_factors`,
        :py:meth:`~.signal.MVATools.plot_bss_loadings`,
        :py:meth:`~.signal.MVATools.plot_bss_results`,

        """
        from hyperspy.signal import BaseSignal

        lr = self.learning_results

        if factors is None:
            if not hasattr(lr, "factors") or lr.factors is None:
                raise AttributeError(
                    "A decomposition must be performed before blind "
                    "source separation, or factors must be provided."
                )
            else:
                if on_loadings:
                    factors = self.get_decomposition_loadings()
                else:
                    factors = self.get_decomposition_factors()

        if hasattr(factors, "compute"):
            # if the factors are lazy, we compute them, which should be fine
            # since we already reduce the dimensionality of the data.
            factors.compute()

        # Check factors
        if not isinstance(factors, BaseSignal):
            raise TypeError(
                "`factors` must be a BaseSignal instance, but an object "
                "of type {} was provided".format(type(factors))
            )

        # Check factor dimensions
        if factors.axes_manager.navigation_dimension != 1:
            raise ValueError(
                "`factors` must have navigation dimension == 1, "
                "but the navigation dimension of the given factors "
                "is {}".format(factors.axes_manager.navigation_dimension)
            )
        elif factors.axes_manager.navigation_size < 2:
            raise ValueError(
                "`factors` must have navigation size"
                "greater than one, but the navigation "
                "size of the given factors is {}".format(
                    factors.axes_manager.navigation_size
                )
            )

        # Check mask dimensions
        if mask is not None:
            ref_shape, space = (
                factors.axes_manager.signal_shape,
                "navigation" if on_loadings else "signal",
            )
            if isinstance(mask, BaseSignal):
                if mask.axes_manager.signal_shape != ref_shape:
                    raise ValueError(
                        (
                            "`mask` shape is not equal to {} shape. "
                            "Mask shape: {}\t{} shape: {}"
                        ).format(
                            space,
                            str(mask.axes_manager.signal_shape),
                            space,
                            str(ref_shape),
                        )
                    )
            if hasattr(mask, "compute"):
                # if the mask is lazy, we compute them, which should be fine
                # since we already reduce the dimensionality of the data.
                mask.compute()

        # Note that we don't check the factor's signal dimension. This is on
        # purpose as an user may like to apply pretreaments that change their
        # dimensionality.

        # The diff_axes are given for the main signal. We need to compute
        # the correct diff_axes for the factors.
        # Get diff_axes index in axes manager
        if diff_axes is not None:
            diff_axes = [
                1 + axis.index_in_axes_manager
                for axis in [self.axes_manager[axis] for axis in diff_axes]
            ]
            if not on_loadings:
                diff_axes = [
                    index - self.axes_manager.navigation_dimension
                    for index in diff_axes
                ]

        # Select components to separate
        if number_of_components is not None:
            comp_list = range(number_of_components)
        elif comp_list is not None:
            number_of_components = len(comp_list)
        else:
            if lr.output_dimension is not None:
                number_of_components = lr.output_dimension
                comp_list = range(number_of_components)
            else:
                raise ValueError("No `number_of_components` or `comp_list` provided")

        factors = stack([factors.inav[i] for i in comp_list])

        # Initialize return_info and print_info
        to_return = None
        to_print = [
            "Blind source separation info:",
            "  number_of_components={}".format(number_of_components),
            "  algorithm={}".format(algorithm),
            "  diff_order={}".format(diff_order),
            "  reverse_component_criterion={}".format(reverse_component_criterion),
            "  whiten_method={}".format(whiten_method),
        ]

        # Apply differences pre-processing if requested.
        if diff_order > 0:
            factors = _get_derivative(
                factors, diff_axes=diff_axes, diff_order=diff_order
            )
            if mask is not None:
                # The following is a little trick to dilate the mask as
                # required when operation on the differences. It exploits the
                # fact that np.diff autimatically "dilates" nans. The trick has
                # a memory penalty which should be low compare to the total
                # memory required for the core application in most cases.
                mask_diff_axes = (
                    [iaxis - 1 for iaxis in diff_axes]
                    if diff_axes is not None
                    else None
                )
                mask.change_dtype("float")
                mask.data[mask.data == 1] = np.nan
                mask = _get_derivative(
                    mask, diff_axes=mask_diff_axes, diff_order=diff_order
                )
                mask.data[np.isnan(mask.data)] = 1
                mask.change_dtype("bool")

        # Unfold in case the signal_dimension > 1
        factors.unfold()
        if mask is not None:
            mask.unfold()
            factors = factors.data.T[np.where(~mask.data)]
        else:
            factors = factors.data.T

        # Center and scale the data
        factors, invsqcovmat = whiten_data(factors, centre=True, method=whiten_method)

        # Perform BSS
        if algorithm == "orthomax":
            _, unmixing_matrix = orthomax(factors, **kwargs)
            lr.bss_node = None

        elif algorithm == "sklearn_fastica":
            if not import_sklearn.sklearn_installed:
                raise ImportError("algorithm='sklearn_fastica' requires scikit-learn")

            if not kwargs.get("tol", False):
                kwargs["tol"] = 1e-10

            lr.bss_node = decomposition_algorithms[algorithm](**kwargs)

            if lr.bss_node.whiten:
                _logger.warning(
                    "HyperSpy performs its own data whitening, "
                    "so it is ignored for sklearn_fastica.",
                )
                lr.bss_node.whiten = False

            lr.bss_node.fit(factors)

            try:
                unmixing_matrix = lr.bss_node.unmixing_matrix_
            except AttributeError:
                # unmixing_matrix was renamed to components in
                # https://github.com/scikit-learn/scikit-learn/pull/858
                unmixing_matrix = lr.bss_node.components_

            to_print.extend(["scikit-learn estimator:", lr.bss_node])
            if return_info:
                to_return = lr.bss_node

        elif algorithm in ["FastICA", "JADE", "CuBICA", "TDSEP"]:
            if not mdp_installed:
                raise ImportError(
                    "algorithm='{}' requires MDP toolbox".format(algorithm)
                )

            temp_function = getattr(mdp.nodes, algorithm + "Node")
            lr.bss_node = temp_function(**kwargs)
            lr.bss_node.train(factors)
            unmixing_matrix = lr.bss_node.get_recmatrix()

            to_print.extend(["mdp estimator:", lr.bss_node])
            if return_info:
                to_return = lr.bss_node

        else:
            raise ValueError("'algorithm' not recognised")

        # Apply the whitening matrix to get the full unmixing matrix
        w = unmixing_matrix @ invsqcovmat

        if lr.explained_variance is not None:
            if hasattr(lr.explained_variance, "compute"):
                lr.explained_variance = lr.explained_variance.compute()

            # The output of ICA is not sorted in any way what makes it
            # difficult to compare results from different unmixings. The
            # following code is an experimental attempt to sort them in a
            # more predictable way
            sorting_indices = np.argsort(
                lr.explained_variance[:number_of_components] @ np.abs(w.T)
            )[::-1]
            w[:] = w[sorting_indices, :]

        lr.unmixing_matrix = w
        lr.on_loadings = on_loadings
        self._unmix_components()
        self._auto_reverse_bss_component(reverse_component_criterion)
        lr.bss_algorithm = algorithm
        lr.bss_node = str(lr.bss_node)

        # Print details about the BSS we just performed
        if print_info:
            print("\n".join([str(pr) for pr in to_print]))

        return to_return

    def normalize_decomposition_components(self, target="factors", function=np.sum):
        """Normalize decomposition components.

        Parameters
        ----------
        target : {"factors", "loadings"}
            Normalize components based on the scale of either the factors or loadings.
        function : numpy universal function, default np.sum
            Each target component is divided by the output of ``function(target)``.
            The function must return a scalar when operating on numpy arrays and
            must have an `axis` argument.

        """
        if target == "factors":
            target = self.learning_results.factors
            other = self.learning_results.loadings
        elif target == "loadings":
            target = self.learning_results.loadings
            other = self.learning_results.factors
        else:
            raise ValueError('target must be "factors" or "loadings"')

        if target is None:
            raise ValueError("This method can only be called after s.decomposition()")

        _normalize_components(target=target, other=other, function=function)

    def normalize_bss_components(self, target="factors", function=np.sum):
        """Normalize BSS components.

        Parameters
        ----------
        target : {"factors", "loadings"}
            Normalize components based on the scale of either the factors or loadings.
        function : numpy universal function, default np.sum
            Each target component is divided by the output of ``function(target)``.
            The function must return a scalar when operating on numpy arrays and
            must have an `axis` argument.

        """
        if target == "factors":
            target = self.learning_results.bss_factors
            other = self.learning_results.bss_loadings
        elif target == "loadings":
            target = self.learning_results.bss_loadings
            other = self.learning_results.bss_factors
        else:
            raise ValueError('target must be "factors" or "loadings"')

        if target is None:
            raise ValueError(
                "This method can only be called after s.blind_source_separation()"
            )

        _normalize_components(target=target, other=other, function=function)

    def reverse_decomposition_component(self, component_number):
        """Reverse the decomposition component.

        Parameters
        ----------
        component_number : list or int
            component index/es

        Examples
        --------
        >>> s = hs.load('some_file')
        >>> s.decomposition(True) # perform PCA
        >>> s.reverse_decomposition_component(1) # reverse IC 1
        >>> s.reverse_decomposition_component((0, 2)) # reverse ICs 0 and 2

        """
        if hasattr(self.learning_results.factors, "compute"):
            _logger.warning(
                "Component(s) {} not reversed, feature not implemented "
                "for lazy computations".format(component_number)
            )
        else:
            target = self.learning_results

            for i in [component_number]:
                _logger.info("Component {} reversed".format(i))
                target.factors[:, i] *= -1
                target.loadings[:, i] *= -1

    def reverse_bss_component(self, component_number):
        """Reverse the independent component.

        Parameters
        ----------
        component_number : list or int
            component index/es

        Examples
        --------
        >>> s = hs.load('some_file')
        >>> s.decomposition(True) # perform PCA
        >>> s.blind_source_separation(3)  # perform ICA on 3 PCs
        >>> s.reverse_bss_component(1) # reverse IC 1
        >>> s.reverse_bss_component((0, 2)) # reverse ICs 0 and 2

        """
        if hasattr(self.learning_results.bss_factors, "compute"):
            _logger.warning(
                "Component(s) {} not reversed, feature not implemented "
                "for lazy computations".format(component_number)
            )
        else:
            target = self.learning_results

            for i in [component_number]:
                _logger.info("Component {} reversed".format(i))
                target.bss_factors[:, i] *= -1
                target.bss_loadings[:, i] *= -1
                target.unmixing_matrix[i, :] *= -1

    def _unmix_components(self, compute=False):
        lr = self.learning_results
        w = lr.unmixing_matrix
        n = len(w)
        if lr.on_loadings:
            lr.bss_loadings = lr.loadings[:, :n] @ w.T
            lr.bss_factors = lr.factors[:, :n] @ np.linalg.inv(w)
        else:
            lr.bss_factors = lr.factors[:, :n] @ w.T
            lr.bss_loadings = lr.loadings[:, :n] @ np.linalg.inv(w)
        if compute:
            lr.bss_factors = lr.bss_factors.compute()
            lr.bss_loadings = lr.bss_loadings.compute()

    def _auto_reverse_bss_component(self, reverse_component_criterion):
        n_components = self.learning_results.bss_factors.shape[1]
        for i in range(n_components):
            if reverse_component_criterion == "factors":
                values = self.learning_results.bss_factors
            elif reverse_component_criterion == "loadings":
                values = self.learning_results.bss_loadings
            else:
                raise ValueError(
                    "`reverse_component_criterion` can take only "
                    "`factor` or `loading` as parameter."
                )
            minimum = np.nanmin(values[:, i])
            maximum = np.nanmax(values[:, i])
            if minimum < 0 and -minimum > maximum:
                self.reverse_bss_component(i)
                _logger.info(
                    "Independent component {} reversed based on the "
                    "{}".format(i, reverse_component_criterion)
                )

    def _calculate_recmatrix(self, components=None, mva_type="decomposition"):
        """Rebuilds data from selected components.

        Parameters
        ----------
        components : None, int, or list of ints
            * If None, rebuilds signal instance from all components
            * If int, rebuilds signal instance from components in range 0-given int
            * If list of ints, rebuilds signal instance from only components in given list
        mva_type : str {'decomposition', 'bss'}
            Decomposition type (not case sensitive)

        Returns
        -------
        Signal instance
            Data built from the given components.

        """

        target = self.learning_results

        if mva_type.lower() == "decomposition":
            factors = target.factors
            loadings = target.loadings.T
        elif mva_type.lower() == "bss":
            factors = target.bss_factors
            loadings = target.bss_loadings.T

        if components is None:
            a = factors @ loadings
            signal_name = "model from {} with {} components".format(
                mva_type, factors.shape[1],
            )
        elif hasattr(components, "__iter__"):
            tfactors = np.zeros((factors.shape[0], len(components)))
            tloadings = np.zeros((len(components), loadings.shape[1]))
            for i in range(len(components)):
                tfactors[:, i] = factors[:, components[i]]
                tloadings[i, :] = loadings[components[i], :]
            a = tfactors @ tloadings
            signal_name = "model from {} with components {}".format(
                mva_type, components
            )
        else:
            a = factors[:, :components] @ loadings[:components, :]
            signal_name = "model from {} with {} components".format(
                mva_type, components
            )

        self._unfolded4decomposition = self.unfold()
        try:
            sc = self.deepcopy()
            sc.data = a.T.reshape(self.data.shape)
            sc.metadata.General.title += " " + signal_name
            if target.mean is not None:
                sc.data += target.mean
        finally:
            if self._unfolded4decomposition:
                self.fold()
                sc.fold()
                self._unfolded4decomposition = False

        return sc

    def get_decomposition_model(self, components=None):
        """Generate model with the selected number of principal components.

        Parameters
        ----------
        components : {None, int, list of ints}, default None
            * If None, rebuilds signal instance from all components
            * If int, rebuilds signal instance from components in range 0-given int
            * If list of ints, rebuilds signal instance from only components in given list

        Returns
        -------
        Signal instance
            A model built from the given components.

        """
        rec = self._calculate_recmatrix(components=components, mva_type="decomposition")
        return rec

    def get_bss_model(self, components=None, chunks="auto"):
        """Generate model with the selected number of independent components.

        Parameters
        ----------
        components : {None, int, list of ints}, default None
            * If None, rebuilds signal instance from all components
            * If int, rebuilds signal instance from components in range 0-given int
            * If list of ints, rebuilds signal instance from only components in given list

        Returns
        -------
        Signal instance
            A model built from the given components.

        """
        lr = self.learning_results
        if self._lazy:
            if isinstance(lr.bss_factors, np.ndarray):
                lr.factors = da.from_array(lr.bss_factors, chunks=chunks)
            if isinstance(lr.bss_factors, np.ndarray):
                lr.loadings = da.from_array(lr.bss_loadings, chunks=chunks)
        rec = self._calculate_recmatrix(components=components, mva_type="bss")
        return rec

    def get_explained_variance_ratio(self):
        """Return explained variance ratio of the PCA components as a Signal1D.

        Read more in the :ref:`User Guide <mva.scree_plot>`.

        Returns
        -------
        s : Signal1D
            Explained variance ratio.

        See Also
        --------
        :py:meth:`~.learn.mva.MVA.decomposition`,
        :py:meth:`~.learn.mva.MVA.plot_explained_variance_ratio`,
        :py:meth:`~.learn.mva.MVA.get_decomposition_loadings`,
        :py:meth:`~.learn.mva.MVA.get_decomposition_factors`.

        """
        from hyperspy._signals.signal1d import Signal1D

        target = self.learning_results
        if target.explained_variance_ratio is None:
            raise AttributeError(
                "The explained_variance_ratio attribute is "
                "`None`, did you forget to perform a PCA "
                "decomposition?"
            )
        s = Signal1D(target.explained_variance_ratio)
        s.metadata.General.title = self.metadata.General.title + "\nPCA Scree Plot"
        s.axes_manager[-1].name = "Principal component index"
        s.axes_manager[-1].units = ""
        return s

    def plot_explained_variance_ratio(
        self,
        n=30,
        log=True,
        threshold=0,
        hline="auto",
        vline=False,
        xaxis_type="index",
        xaxis_labeling=None,
        signal_fmt=None,
        noise_fmt=None,
        fig=None,
        ax=None,
        **kwargs
    ):
        """Plot the decomposition explained variance ratio vs index number.

        This is commonly known as a scree plot.

        Read more in the :ref:`User Guide <mva.scree_plot>`.

        Parameters
        ----------
        n : int or None
            Number of components to plot. If None, all components will be plot
        log : bool, default True
            If True, the y axis uses a log scale.
        threshold : float or int
            Threshold used to determine how many components should be
            highlighted as signal (as opposed to noise).
            If a float (between 0 and 1), ``threshold`` will be
            interpreted as a cutoff value, defining the variance at which to
            draw a line showing the cutoff between signal and noise;
            the number of signal components will be automatically determined
            by the cutoff value.
            If an int, ``threshold`` is interpreted as the number of
            components to highlight as signal (and no cutoff line will be
            drawn)
        hline: {'auto', True, False}
            Whether or not to draw a horizontal line illustrating the variance
            cutoff for signal/noise determination. Default is to draw the line
            at the value given in ``threshold`` (if it is a float) and not
            draw in the case  ``threshold`` is an int, or not given.
            If True, (and ``threshold`` is an int), the line will be drawn
            through the last component defined as signal.
            If False, the line will not be drawn in any circumstance.
        vline: bool, default False
            Whether or not to draw a vertical line illustrating an estimate of
            the number of significant components. If True, the line will be
            drawn at the the knee or elbow position of the curve indicating the
            number of significant components.
            If False, the line will not be drawn in any circumstance.
        xaxis_type : {'index', 'number'}
            Determines the type of labeling applied to the x-axis.
            If ``'index'``, axis will be labeled starting at 0 (i.e.
            "pythonic index" labeling); if ``'number'``, it will start at 1
            (number labeling).
        xaxis_labeling : {'ordinal', 'cardinal', None}
            Determines the format of the x-axis tick labels. If ``'ordinal'``,
            "1st, 2nd, ..." will be used; if ``'cardinal'``, "1, 2,
            ..." will be used. If None, an appropriate default will be
            selected.
        signal_fmt : dict
            Dictionary of matplotlib formatting values for the signal
            components
        noise_fmt : dict
            Dictionary of matplotlib formatting values for the noise
            components
        fig : matplotlib figure or None
            If None, a default figure will be created, otherwise will plot
            into fig
        ax : matplotlib ax (subplot) or None
            If None, a default ax will be created, otherwise will plot into ax
        **kwargs
            remaining keyword arguments are passed to ``matplotlib.figure()``

        Returns
        -------
        ax : matplotlib.axes
            Axes object containing the scree plot

        Example
        -------
        To generate a scree plot with customized symbols for signal vs.
        noise components and a modified cutoff threshold value:

        >>> s = hs.load("some_spectrum_image")
        >>> s.decomposition()
        >>> s.plot_explained_variance_ratio(n=40,
        >>>                                 threshold=0.005,
        >>>                                 signal_fmt={'marker': 'v',
        >>>                                             's': 150,
        >>>                                             'c': 'pink'}
        >>>                                 noise_fmt={'marker': '*',
        >>>                                             's': 200,
        >>>                                             'c': 'green'})

        See Also
        --------
        :py:meth:`~.learn.mva.MVA.decomposition`,
        :py:meth:`~.learn.mva.MVA.get_explained_variance_ratio`,
        :py:meth:`~.signal.MVATools.get_decomposition_loadings`,
        :py:meth:`~.signal.MVATools.get_decomposition_factors`

        """
        s = self.get_explained_variance_ratio()

        n_max = len(self.learning_results.explained_variance_ratio)
        if n is None:
            n = n_max
        elif n > n_max:
            _logger.info("n is too large, setting n to its maximal value.")
            n = n_max

        # Determine right number of components for signal and cutoff value
        if isinstance(threshold, float):
            if not 0 < threshold < 1:
                raise ValueError("Variance threshold should be between 0 and" " 1")
            # Catch if the threshold is less than the minimum variance value:
            if threshold < s.data.min():
                n_signal_pcs = n
            else:
                n_signal_pcs = np.where((s < threshold).data)[0][0]
        else:
            n_signal_pcs = threshold
            if n_signal_pcs == 0:
                hline = False

        if vline:
            if self.learning_results.number_significant_components is None:
                vline = False
            else:
                index_number_significant_components = (
                    self.learning_results.number_significant_components - 1
                )
        else:
            vline = False

        # Handling hline logic
        if hline == "auto":
            # Set cutoff to threshold if float
            if isinstance(threshold, float):
                cutoff = threshold
            # Turn off the hline otherwise
            else:
                hline = False
        # If hline is True and threshold is int, set cutoff at value of last
        # signal component
        elif hline:
            if isinstance(threshold, float):
                cutoff = threshold
            elif n_signal_pcs > 0:
                cutoff = s.data[n_signal_pcs - 1]
        # Catches hline==False and hline==True (if threshold not given)
        else:
            hline = False

        # Some default formatting for signal markers
        if signal_fmt is None:
            signal_fmt = {
                "c": "#C24D52",
                "linestyle": "",
                "marker": "^",
                "markersize": 10,
                "zorder": 3,
            }

        # Some default formatting for noise markers
        if noise_fmt is None:
            noise_fmt = {
                "c": "#4A70B0",
                "linestyle": "",
                "marker": "o",
                "markersize": 10,
                "zorder": 3,
            }

        # Sane defaults for xaxis labeling
        if xaxis_labeling is None:
            xaxis_labeling = "cardinal" if xaxis_type == "index" else "ordinal"

        axes_titles = {
            "y": "Proportion of variance",
            "x": "Principal component {}".format(xaxis_type),
        }

        if n < s.axes_manager[-1].size:
            s = s.isig[:n]

        if fig is None:
            fig = plt.figure(**kwargs)

        if ax is None:
            ax = fig.add_subplot(111)

        if log:
            ax.set_yscale("log")

        if hline:
            ax.axhline(cutoff, linewidth=2, color="gray", linestyle="dashed", zorder=1)

        if vline:
            ax.axvline(
                index_number_significant_components,
                linewidth=2,
                color="gray",
                linestyle="dashed",
                zorder=1,
            )

        index_offset = 0
        if xaxis_type == "number":
            index_offset = 1

        if n_signal_pcs == n:
            ax.plot(
                range(index_offset, index_offset + n), s.isig[:n].data, **signal_fmt
            )
        elif n_signal_pcs > 0:
            ax.plot(
                range(index_offset, index_offset + n_signal_pcs),
                s.isig[:n_signal_pcs].data,
                **signal_fmt,
            )
            ax.plot(
                range(index_offset + n_signal_pcs, index_offset + n),
                s.isig[n_signal_pcs:n].data,
                **noise_fmt,
            )
        else:
            ax.plot(range(index_offset, index_offset + n), s.isig[:n].data, **noise_fmt)

        if xaxis_labeling == "cardinal":
            ax.xaxis.set_major_formatter(FuncFormatter(lambda x, p: ordinal(x)))

        ax.set_ylabel(axes_titles["y"])
        ax.set_xlabel(axes_titles["x"])
        ax.xaxis.set_major_locator(MaxNLocator(integer=True, min_n_ticks=1))
        ax.margins(0.05)
        ax.autoscale()
        ax.set_title(s.metadata.General.title, y=1.01)

        return ax

    def plot_cumulative_explained_variance_ratio(self, n=50):
        """Plot cumulative explained variance up to n principal components.

        Parameters
        ----------
        n : int
            Number of principal components to show.

        Returns
        -------
        ax : matplotlib.axes
            Axes object containing the cumulative explained variance plot.

        See Also
        --------
        :py:meth:`~.learn.mva.MVA.plot_explained_variance_ratio`,

        """
        target = self.learning_results
        if n > target.explained_variance.shape[0]:
            n = target.explained_variance.shape[0]
        cumu = np.cumsum(target.explained_variance) / np.sum(target.explained_variance)
        fig = plt.figure()
        ax = fig.add_subplot(111)
        ax.scatter(range(n), cumu[:n])
        ax.set_xlabel("Principal component")
        ax.set_ylabel("Cumulative explained variance ratio")
        plt.draw()

        return ax

    def normalize_poissonian_noise(self, navigation_mask=None, signal_mask=None):
        """Normalize the signal under the assumption of Poisson noise.

        Scales the signal using to "normalize" the Poisson data for
        subsequent decomposition analysis [Keenan2004]_.

        Parameters
        ----------
        navigation_mask : {None, boolean numpy array}, default None
            Optional mask applied in the navigation axis.
        signal_mask : {None, boolean numpy array}, default None
            Optional mask applied in the signal axis.

        """
        _logger.info("Scaling the data to normalize Poissonian noise")
        with self.unfolded():
            # The rest of the code assumes that the first data axis
            # is the navigation axis. We transpose the data if that
            # is not the case.
            if self.axes_manager[0].index_in_array == 0:
                dc = self.data
            else:
                dc = self.data.T

            if navigation_mask is None:
                navigation_mask = slice(None)
            else:
                navigation_mask = ~navigation_mask.ravel()
            if signal_mask is None:
                signal_mask = slice(None)
            else:
                signal_mask = ~signal_mask

            # Check non-negative
            if dc[:, signal_mask][navigation_mask, :].min() <= 0.0:
                raise ValueError(
                    "Negative values found in data!\n"
                    "Are you sure that the data follow a Poisson distribution?"
                )

            # Rescale the data to normalize the Poisson noise
            aG = dc[:, signal_mask][navigation_mask, :].sum(1).squeeze()
            bH = dc[:, signal_mask][navigation_mask, :].sum(0).squeeze()

            self._root_aG = np.sqrt(aG)[:, np.newaxis]
            self._root_bH = np.sqrt(bH)[np.newaxis, :]

            # We ignore numpy's warning when the result of an
            # operation produces nans - instead we set 0/0 = 0
            with np.errstate(divide="ignore", invalid="ignore"):
                dc[:, signal_mask][navigation_mask, :] /= self._root_aG * self._root_bH
                dc[:, signal_mask][navigation_mask, :] = np.nan_to_num(
                    dc[:, signal_mask][navigation_mask, :]
                )

    def undo_treatments(self):
        """Undo Poisson noise normalization and other pre-treatments.

        Only valid if calling ``s.decomposition(..., copy=True)``.
        """
        if hasattr(self, "_data_before_treatments"):
            _logger.info("Undoing data pre-treatments")
            self.data[:] = self._data_before_treatments
            del self._data_before_treatments
        else:
            raise AttributeError(
                "Unable to undo data pre-treatments! Be sure to"
                "set `copy=True` when calling s.decomposition()."
            )

    def estimate_elbow_position(self, explained_variance_ratio=None, max_points=20):
        """Estimate the elbow position of a scree plot curve.

        Used to estimate the number of significant components in
        a PCA variance ratio plot or other "elbow" type curves.

        Find a line between first and last point on the scree plot.
        With a classic elbow scree plot, this line more or less
        defines a triangle. The elbow should be the point which
        is the furthest distance from this line. For more details,
        see [Satopää2011]_.

        Parameters
        ----------
        explained_variance_ratio : {None, numpy array}
            Explained variance ratio values that form the scree plot.
            If None, uses the ``explained_variance_ratio`` array stored
            in ``s.learning_results``, so a decomposition must have
            been performed first.
        max_points : int
            Maximum number of points to consider in the calculation.

        Returns
        -------
        elbow position : int
            Index of the elbow position in the input array. Due to
            zero-based indexing, the number of significant components
            is `elbow_position + 1`.

        References
        ----------
        .. [Satopää2011] V. Satopää, J. Albrecht, D. Irwin, and B. Raghavan.
            "Finding a “Kneedle” in a Haystack: Detecting Knee Points in
            System Behavior,. 31st International Conference on Distributed
            Computing Systems Workshops, pp. 166-171, June 2011.

        See Also
        --------
        :py:meth:`~.learn.mva.MVA.get_explained_variance_ratio`,
        :py:meth:`~.learn.mva.MVA.plot_explained_variance_ratio`,

        """
        if explained_variance_ratio is None:
            if self.learning_results.explained_variance_ratio is None:
                raise ValueError(
                    "A decomposition must be performed before calling "
                    "estimate_elbow_position(), or pass a numpy array directly."
                )

            curve_values = self.learning_results.explained_variance_ratio
        else:
            curve_values = explained_variance_ratio

        max_points = min(max_points, len(curve_values) - 1)

        # Clipping the curve_values from below with a v.small
        # number avoids warnings below when taking np.log(0)
        curve_values_adj = np.clip(curve_values, 1e-30, None)

        x1 = 0
        x2 = max_points

        y1 = np.log(curve_values_adj[0])
        y2 = np.log(curve_values_adj[max_points])

        xs = np.arange(max_points)
        ys = np.log(curve_values_adj[:max_points])

        numer = np.abs((x2 - x1) * (y1 - ys) - (x1 - xs) * (y2 - y1))
        denom = np.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2)
        distance = np.nan_to_num(numer / denom)

        # Point with the largest distance is the "elbow"
        # (remember that np.argmax returns the FIRST instance)
        elbow_position = np.argmax(distance)

        return elbow_position


class LearningResults(object):
    """Stores the parameters and results from a decomposition."""

    def __init__(self):
        # Decomposition
        self.factors = None
        self.loadings = None
        self.explained_variance = None
        self.explained_variance_ratio = None
        self.number_significant_components = None
        self.decomposition_algorithm = None
        self.poissonian_noise_normalized = None
        self.output_dimension = None
        self.mean = None
        self.centre = None
        # Unmixing
        self.bss_algorithm = None
        self.unmixing_matrix = None
        self.bss_factors = None
        self.bss_loadings = None
        # Shape
        self.unfolded = None
        self.original_shape = None
        # Masks
        self.navigation_mask = None
        self.signal_mask = None

    def save(self, filename, overwrite=None):
        """Save the result of the decomposition and demixing analysis.

        Parameters
        ----------
        filename : string
            Path to save the results to.
        overwrite : {True, False, None}, default None
            If True, overwrite the file if it exists.
            If None (default), prompt user if file exists.

        """
        kwargs = {}
        for attribute in [
            v
            for v in dir(self)
            if not isinstance(getattr(self, v), types.MethodType)
            and not v.startswith("_")
        ]:
            kwargs[attribute] = self.__getattribute__(attribute)
        # Check overwrite
        if overwrite is None:
            overwrite = io_tools.overwrite(filename)
        # Save, if all went well!
        if overwrite:
            np.savez(filename, **kwargs)
            _logger.info("Saved results to {}".format(filename))

    def load(self, filename):
        """Load the results of a previous decomposition and demixing analysis.

        Parameters
        ----------
        filename : string
            Path to load the results from.

        """
        decomposition = np.load(filename, allow_pickle=True)

        for key, value in decomposition.items():
            if value.dtype == np.dtype("object"):
                value = None
            # Unwrap values stored as 0D numpy arrays to raw datatypes
            if isinstance(value, np.ndarray) and value.ndim == 0:
                value = value.item()
            setattr(self, key, value)

        _logger.info("Loaded results from {}".format(filename))

        # For compatibility with old version
        if hasattr(self, "algorithm"):
            self.decomposition_algorithm = self.algorithm
            del self.algorithm
        if hasattr(self, "V"):
            self.explained_variance = self.V
            del self.V
        if hasattr(self, "w"):
            self.unmixing_matrix = self.w
            del self.w
        if hasattr(self, "variance2one"):
            del self.variance2one
        if hasattr(self, "centered"):
            del self.centered
        if hasattr(self, "pca_algorithm"):
            self.decomposition_algorithm = self.pca_algorithm
            del self.pca_algorithm
        if hasattr(self, "ica_algorithm"):
            self.bss_algorithm = self.ica_algorithm
            del self.ica_algorithm
        if hasattr(self, "v"):
            self.loadings = self.v
            del self.v
        if hasattr(self, "scores"):
            self.loadings = self.scores
            del self.scores
        if hasattr(self, "pc"):
            self.loadings = self.pc
            del self.pc
        if hasattr(self, "ica_scores"):
            self.bss_loadings = self.ica_scores
            del self.ica_scores
        if hasattr(self, "ica_factors"):
            self.bss_factors = self.ica_factors
            del self.ica_factors

        # Log summary
        self.summary()

    def __repr__(self):
        """Summarize the decomposition and demixing parameters."""
        return self.summary()

    def summary(self):
        """Summarize the decomposition and demixing parameters.

        Returns
        -------
        str
            String summarizing the learning parameters.

        """

        summary_str = (
            "Decomposition parameters\n"
            "------------------------\n"
            "normalize_poissonian_noise={}\n"
            "algorithm={}\n"
            "output_dimension={}\n"
            "centre={}"
        ).format(
            self.poissonian_noise_normalized,
            self.decomposition_algorithm,
            self.output_dimension,
            self.centre,
        )

        if self.bss_algorithm is not None:
            summary_str += (
                "\n\nDemixing parameters\n"
                "-------------------\n"
                "algorithm={}\n"
                "n_components={}"
            ).format(self.bss_algorithm, len(self.unmixing_matrix))

        _logger.info(summary_str)

        return summary_str

    def crop_decomposition_dimension(self, n, compute=False):
        """Crop the score matrix up to the given number.

        It is mainly useful to save memory and reduce the storage size

        Parameters
        ----------
        n : int
            Number of components to keep.
        compute : bool, default False
           If True and the decomposition results are lazy,
           also compute the results.

        """
        _logger.info("Trimming results to {} dimensions".format(n))
        self.loadings = self.loadings[:, :n]
        if self.explained_variance is not None:
            self.explained_variance = self.explained_variance[:n]
        self.factors = self.factors[:, :n]
        if compute:
            self.loadings = self.loadings.compute()
            self.factors = self.factors.compute()
            if self.explained_variance is not None:
                self.explained_variance = self.explained_variance.compute()

    def _transpose_results(self):
        (self.factors, self.loadings, self.bss_factors, self.bss_loadings) = (
            self.loadings,
            self.factors,
            self.bss_loadings,
            self.bss_factors,
        )
