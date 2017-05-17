"""ML-ENSEMBLE

:author: Sebastian Flennerhag
:copyright: 2017
:licence: MIT

Base class for estimation.
"""

import os
from abc import ABCMeta, abstractmethod
from time import sleep

import numpy as np

from ..externals.joblib import delayed
from ..utils import (check_is_fitted,
                     pickle_load,
                     pickle_save,
                     print_time,
                     safe_print)
from ..utils.exceptions import (NotFittedError,
                                ParallelProcessingError,
                                ParallelProcessingWarning)

try:
    from time import perf_counter as time_
except ImportError:
    from time import time as time_

import warnings


class BaseEstimator(object):

    """Base class for estimating a layer in parallel.

    Estimation class to be used as based for a layer estimation engined that
    is callable by the :class:`ParallelProcess` job manager.

    A subclass must implement a ``_format_instance_list`` method for
    building a list of preprocessing cases and a list of estimators that
    will be iterated over in the call to :class:`joblib.Parallel`,
    and a ``_get_col_id`` method for assigning a unique column and if
    applicable, row slice, to each estimator in the estimator list.
    The subclass ``__init__`` method should be a call to ``super``.

    Parameters
    ----------
    layer : :class:`Layer`
        layer to be estimated

    dual : bool
        whether to estimate transformers separately from estimators: else,
        the lists will be combined in one parallel for-loop.
    """

    __metaclass__ = ABCMeta

    __slots__ = ['verbose', 'layer', 'raise_', 'name', 'classes', 'proba',
                 'ivals', 'dual', 'e', 't', 'c', 'scorer']

    @abstractmethod
    def __init__(self, layer, dual=True):
        self.layer = layer

        # Copy some layer parameters to ease notation
        self.verbose = self.layer.verbose
        self.raise_ = self.layer.raise_on_exception
        self.name = self.layer.name
        self.proba = self.layer.proba
        self.scorer = self.layer.scorer
        self.ivals = (getattr(layer, 'ival', 0.1), getattr(layer, 'lim', 600))

        # Set estimator and transformer lists to loop over, and collect
        # estimator column ids for the prediction matrix
        self.e, self.t = self._format_instance_list()
        self.c = self._get_col_id()

        self.dual = dual

    @abstractmethod
    def _format_instance_list(self):
        """Formatting layer's estimator and preprocessing for parallel loop."""

    @abstractmethod
    def _get_col_id(self):
        """Assign unique col_id to every estimator."""

    def _assemble(self, dir):
        """Store fitted transformer and estimators in the layer."""
        self.layer.preprocessing_ = _assemble(dir, self.t, 't')
        self.layer.estimators_, s = _assemble(dir, self.e, 'e')

        if self.scorer is not None and self.layer.cls is not 'full':
            self.layer.scores_ = self._build_scores(s)

    def _build_scores(self, s):
        """Build a cv-score mapping."""
        scores = dict()

        # Build shell dictionary with main estimators as keys
        for k, v in s[:self.layer.n_pred]:
            case_name, est_name = k.split('___')

            if case_name == '':
                name = est_name
            else:
                name = '%s__%s' % (case_name, est_name)

            scores[name] = []

        # Populate with list of scores from folds
        for k, v in s[self.layer.n_pred:]:
            case_name, est_name = k.split('___')

            est_name = '__'.join(est_name.split('__')[:-1])

            if '__' not in case_name:
                name = est_name
            else:
                case_name = case_name.split('__')[0]
                name = '%s__%s' % (case_name, est_name)

            scores[name].append(v)

        # Aggregate to get cross-validated mean scores
        for k, v in scores.items():
            scores[k] = (np.mean(v), np.std(v))

        return scores

    def fit(self, X, y, P, dir, parallel):
        """Fit layer through given attribute."""
        if self.verbose:
            printout = "stderr" if self.verbose < 50 else "stdout"
            safe_print('Fitting %s' % self.name, file=printout)
            t0 = time_()

        # Auxiliary variables
        preprocess = self.t is not None
        pred_method = 'predict' if not self.proba else 'predict_proba'

        if y.shape[0] > X.shape[0]:
            # This is legal if X is a prediction matrix generated by predicting
            # only a subset of the original training set.
            # Since indexing is strictly monotonic, we can simply discard
            # the first observations in y to get the corresponding labels.
            rebase = y.shape[0] - X.shape[0]
            y = y[rebase:]

        if self.dual:
            if preprocess:
                parallel(delayed(fit_trans)(dir=dir,
                                            case=case,
                                            inst=instance_list,
                                            x=X,
                                            y=y,
                                            idx=tri,
                                            name=self.name)
                         for i, (case, tri, _, instance_list)
                         in enumerate(self.t))

            parallel(delayed(fit_est)(dir=dir,
                                      case=case,
                                      inst_name=inst_name,
                                      inst=instance,
                                      x=X,
                                      y=y,
                                      pred=P if tei is not None else None,
                                      idx=(tri, tei, self.c[case, inst_name]),
                                      name=self.name,
                                      raise_on_exception=self.raise_,
                                      preprocess=preprocess,
                                      ivals=self.ivals,
                                      attr=pred_method,
                                      scorer=self.scorer)
                     for case, tri, tei, instance_list in self.e
                     for inst_name, instance in instance_list)

        else:
            parallel(delayed(_fit)(dir=dir,
                                   case=case,
                                   inst_name=inst_name,
                                   inst=instance,
                                   x=X,
                                   y=y,
                                   pred=P if tei is not None else None,
                                   idx=(tri, tei, self.c[case, inst_name])
                                   if inst_name != '__trans__' else tri,
                                   name=self.layer.name,
                                   raise_on_exception=self.raise_,
                                   preprocess=preprocess,
                                   ivals=self.ivals,
                                   scorer=self.scorer)
                     for case, tri, tei, inst_list in _wrap(self.t) + self.e
                     for inst_name, instance in inst_list)

        # Load instances from cache and store as layer attributes
        # Typically, as layer.estimators_, layer.preprocessing_
        self._assemble(dir)

        if self.verbose:
            print_time(t0, '%s Done' % self.name, file=printout)

    def predict(self, X, P, parallel):
        """Predict with fitted layer with either full or fold ests."""
        self._check_fitted()

        if self.verbose:
            printout = "stderr" if self.verbose < 50 else "stdout"
            safe_print('Predicting %s' % self.name, file=printout)
            t0 = time_()

        pred_method = 'predict' if not self.proba else 'predict_proba'

        # Collect estimators, either fitted on full data or folds
        prep, ests = self._retrieve('full')

        parallel(delayed(predict_est)(case=case,
                                      tr_list=prep[case]
                                      if prep is not None else [],
                                      inst_name=inst_name,
                                      est=est,
                                      xtest=X,
                                      pred=P,
                                      col=col,
                                      name=self.name,
                                      attr=pred_method)
                 for case, (inst_name, est, (_, col)) in ests)

        if self.verbose:
            print_time(t0, '%s Done' % self.name, file=printout)

    def transform(self, X, P, parallel):
        """Transform training data with fold-estimators from fit call."""
        self._check_fitted()

        if self.verbose:
            printout = "stderr" if self.verbose < 50 else "stdout"
            safe_print('Transforming %s' % self.name, file=printout)
            t0 = time_()

        pred_method = 'predict' if not self.proba else 'predict_proba'

        # Collect estimators, either fitted on full data or folds
        prep, ests = self._retrieve('fold')

        parallel(delayed(predict_fold_est)(case=case,
                                           tr_list=prep[case]
                                           if prep is not None else [],
                                           inst_name=est_name,
                                           est=est,
                                           xtest=X,
                                           pred=P,
                                           idx=idx,
                                           name=self.name,
                                           attr=pred_method)
                 for case, (est_name, est, idx) in ests)

        if self.verbose:
            print_time(t0, '%s Done' % self.name, file=printout)

    def _check_fitted(self):
        """Utility function for checking that fitted estimators exist."""
        check_is_fitted(self.layer, "estimators_")

        assert isinstance(self.layer.estimators_, list)
        if len(self.layer.estimators_) == 0:
            raise NotFittedError("No estimators successfully fitted.")

    def _retrieve(self, s):
        """Get transformers and estimators fitted on folds or on full data."""
        n_pred = self.layer.n_pred
        n_prep = max(self.layer.n_prep, 1)

        if s == 'full':
            # If full, grab the first n_pred estimators, and the first
            # n_prep preprocessing pipelines, which are fitted on
            # the full training data. We take max on n_prep to avoid getting
            # empty preprocessing_ slice when n_prep = 0 when no preprocessing.
            ests = self.layer.estimators_[:n_pred]

            if self.layer.preprocessing_ is None:
                prep = None
            else:
                prep = dict(self.layer.preprocessing_[:n_prep])

        elif s == 'fold':
            # If fold, grab the estimators after n_pred, and the preprocessing
            # pipelines after n_prep, which are fitted on folds of the
            # training data.
            ests = self.layer.estimators_[n_pred:]

            if self.layer.preprocessing_ is None:
                prep = None
            else:
                prep = dict(self.layer.preprocessing_[n_prep:])

        else:
            raise ValueError("Argument not understood. Only 'full' and 'fold' "
                             "are acceptable argument values.")

        return prep, ests


###############################################################################
def _wrap(folded_list, name='__trans__'):
    """Wrap the folded transformer list.

    wraps a folded transformer list so that the ``tr_list`` appears as
    one estimator with a specified name. Since all ``tr_list``s have the
    same name, it can be used to select a transformation function or an
    estimation function in a combined parallel fitting loop.
    """
    return [(case, tri, None, [(name, instance_list)]) for
            case, tri, tei, instance_list in folded_list]


def _slice_array(x, y, idx, r = 0):
    """Build training array index and slice data."""
    if idx is None:
        return x, y
    else:
        # Check if the idx is a tuple and if so, whether it can be made
        # into a simple slice
        if isinstance(idx[0], tuple):
            if len(idx[0]) > 1:
                # Advanced indexing is required. This will trigger a copy
                # of the slice in question to be made
                simple_slice = False
                idx = np.hstack([np.arange(t0 - r, t1 - r) for t0, t1 in idx])
                x = x[idx]
                y = y[idx] if y is not None else y
            else:
                # The tuple is of the form ((a, b),) and can be made
                # into a simple (a, b) tuple for which basic slicing applies
                # which allows a view to be returned instead of a copy
                simple_slice = True
                idx = idx[0]
        else:
            # Index tuples of the form (a, b) allows simple slicing
            simple_slice = True

        if simple_slice:
            x = x[slice(idx[0] - r, idx[1] - r)]
            y = y[slice(idx[0] - r, idx[1] - r)] if y is not None else y

    return x, y


def _assign_predictions(pred, p, tei, col, n):
    """Assign predictions to memmaped prediction array."""
    r = n - pred.shape[0]

    if isinstance(tei[0], tuple):
        if len(tei) > 1:
            idx = np.hstack([np.arange(t0 - r, t1 - r) for t0, t1 in tei])
        else:
            tei = tei[0]
            idx = slice(tei[0] - r, tei[1] - r)
    else:
        idx = slice(tei[0] - r, tei[1] - r)

    if len(p.shape) == 1:
        pred[idx, col] = p
    else:
        pred[(idx, slice(col, col + p.shape[1]))] = p


def _assemble(dir, instance_list, suffix):
    """Utility for loading fitted instances."""
    if suffix is 't':
        if instance_list is None:
            return

        return [(tup[0],
                 pickle_load(os.path.join(dir, '%s__%s' % (tup[0], suffix))))
                for tup in instance_list]
    else:
        # We iterate over estimators to split out the estimator info and the
        # scoring info (if any)
        ests_ = []
        scores_ = []
        for tup in instance_list:
            for etup in tup[-1]:
                f = os.path.join(dir, '%s__%s__%s' % (tup[0], etup[0], suffix))
                loaded = pickle_load(f)

                # split out the scores, the final element in the l tuple
                ests_.append((tup[0], loaded[:-1]))

                case = '%s___' % tup[0] if tup[0] is not None else '___'
                scores_.append((case + etup[0], loaded[-1]))

        return ests_, scores_


###############################################################################
def predict_est(case, tr_list, inst_name, est, xtest, pred, col, name, attr):
    """Method for predicting with fitted transformers and estimators."""
    # Transform input
    for tr_name, tr in tr_list:
        xtest = tr.transform(xtest)

    # Predict into memmap
    # Here, we coerce errors on failed predictions - all predictors that
    # survive into the estimators_ attribute of a layer should be able to
    # predict, otherwise the subsequent layer will get corrupt input.
    p = getattr(est, attr)(xtest)

    if len(p.shape) == 1:
        pred[:, col] = p
    else:
        pred[:, col:(col + p.shape[1])] = p


def predict_fold_est(case, tr_list, inst_name, est, xtest, pred, idx, name,
                     attr):
    """Method for predicting with transformers and estimators from fit call."""
    tei = idx[0]
    col = idx[1]
    n = xtest.shape[0]

    xtest, _ = _slice_array(xtest, None, tei)

    for tr_name, tr in tr_list:
        xtest = tr.transform(xtest)

    # Predict into memmap
    # Here, we coerce errors on failed predictions - all predictors that
    # survive into the estimators_ attribute of a layer should be able to
    # predict, otherwise the subsequent layer will get corrupt input.
    p = getattr(est, attr)(xtest)

    # Assign predictions to matrix
    _assign_predictions(pred, p, tei, col, n)


def fit_trans(dir, case, inst, x, y, idx, name):
    """Fit transformers and write to cache."""
    x, y = _slice_array(x, y, idx)

    out = []
    for tr_name, tr in inst:
        # Fit transformer
        tr.fit(x, y)

        # If more than one step, transform input for next step.
        # This step triggers a copy of x to be created
        if len(inst) > 1:
            x, y = _transform(tr, x, y)

        out.append((tr_name, tr))

    # Write transformer list to cache
    f = os.path.join(dir, '%s__t' % case)
    pickle_save(out, f)


def fit_est(dir, case, inst_name, inst, x, y, pred, idx, raise_on_exception,
            preprocess, name, ivals, attr, scorer=None):
    """Fit estimator and write to cache along with predictions."""
    # Have to be careful in prepping data for estimation.
    # We need to slice memmap and convert to a proper array - otherwise
    # estimators can store results memmaped to the cache, which will
    # prevent the garbage collector from releasing the memmaps from memory
    # after estimation
    n = x.shape[0]

    xtrain, ytrain = _slice_array(x, y, idx[0])

    # Load transformers
    if preprocess:
        f = os.path.join(dir, '%s__t' % case)
        tr_list = _load_trans(f, case, ivals, raise_on_exception)
    else:
        tr_list = []

    # Transform input
    # This will trigger copying of x and z
    for tr_name, tr in tr_list:
        xtrain, ytrain = _transform(tr, xtrain, ytrain)

    # Fit estimator
    inst.fit(xtrain, ytrain)

    # Predict if asked
    # The predict loop is kept separate to allow overwrite of x, thus keeping
    # only one subset of X in memory at any given time
    if idx[1] is not None:
        tei = idx[1]
        col = idx[2]

        xtest, ytest = _slice_array(x, y, tei)

        for tr_name, tr in tr_list:
            xtest = tr.transform(xtest)

        p = getattr(inst, attr)(xtest)

        # Assign predictions to matrix
        _assign_predictions(pred, p, tei, col, n)

        # Score predictions if applicable
        try:
            s = scorer(ytest, p)
        except Exception:
            s = None

        idx = idx[1:] # format as (tei, col)
    else:
        idx = (None, idx[2]) # (tei, col), with tei = all observations
        s = None

    f = os.path.join(dir, '%s__%s__e' % (case, inst_name))
    pickle_save((inst_name, inst, idx, s), f)


def _fit(**kwargs):
    """Wrapper to select fit_est or fit_trans."""
    f = fit_trans if kwargs['inst_name'] == '__trans__' else fit_est
    f(**{k: v for k, v in kwargs.items() if k in f.__code__.co_varnames})


###############################################################################
def _transform(tr, x, y):
    """Try transforming with X and y. Else, transform with only X."""
    try:
        x = tr.transform(x, y)
        if isinstance(x, (tuple, list)):
            x, y = x

    except TypeError:
        x = tr.transform(x)

    return x, y


def _load_trans(dir, case, ivals, raise_on_exception):
    """Try loading transformers, and handle exception if not ready yet."""
    s = ivals[0]
    lim = ivals[1]
    try:
        # Assume file exists
        return pickle_load(dir)
    except (OSError, IOError) as exc:
        # We would expect an OSError, but Python 2.7 we get an IOError
        msg = str(exc)
        error_msg = ("The file %s cannot be found after %i seconds of "
                     "waiting. Check that time to fit transformers is "
                     "sufficiently fast to complete fitting before "
                     "fitting estimators. Consider reducing the "
                     "preprocessing intensity in the ensemble, or "
                     "increase the '__lim__' attribute to wait extend "
                     "period of waiting on transformation to complete."
                     " Details:\n%r")

        # Wait and check if transformer is readied.
        ts = time_()
        while not os.path.exists(dir):

            sleep(s)

            if time_() - ts > lim:
                # If timeout limit is reached, raise error
                if raise_on_exception:
                    raise ParallelProcessingError(error_msg % (dir, lim, msg))

                warnings.warn("Transformer %s not found in cache (%s). "
                              "Will check every %.1f seconds for %i seconds "
                              "before aborting. " % (case, dir, s, lim),
                              ParallelProcessingWarning)

                # If not raise_on_exception, we set it to True now to ensure
                # a second timeout aborts the job
                raise_on_exception = True
                ts = time_()

        return pickle_load(dir)
