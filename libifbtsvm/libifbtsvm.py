
from typing import Dict, Generator, Optional, Tuple, Union

import numpy as np

from joblib import (  # type: ignore
    delayed,
    Parallel,
)
from numpy import linalg
from sklearn.metrics import accuracy_score
from sklearn.svm import SVC

from libifbtsvm.functions import (
    fuzzy_membership,
    train_model,
)
from libifbtsvm.functions.ctrain_model import train_model as ctrain_model
from libifbtsvm.models.ifbtsvm import (
    ClassificationModel,
    FuzzyMembership,
    Hyperparameters,
    Hyperplane,
)

TrainingSet = Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]
DAGSubSet = Union[TrainingSet, Generator[TrainingSet, None, None]]


class iFBTSVM(SVC):

    def __init__(self, parameters: Hyperparameters, *args, n_jobs=1, **kwargs):
        super().__init__(*args, **kwargs)
        self.parameters = parameters
        self._classifiers: Dict = {}
        self.n_jobs = n_jobs
        self.kernel = parameters.kernel

    @classmethod
    def _compute_score(cls, score, c):
        """

        :param score:
        :param c:
        :return:
        """
        if score is None:
            score = np.asarray(c)
            sc = np.ones(len(c))
            score = np.array((score, sc))

        else:
            res, indices_score, indices_c = np.intersect1d(score[0], np.asarray(c), return_indices=True)
            score[1][indices_score] += 1
            diff = np.setdiff1d(score[0], np.asarray(c))

            if diff.any():
                _zdiff = np.ones(len(diff))
                new_score = np.unique(np.array((diff, _zdiff)))

                np.append(score[0], [new_score])
                np.append(score[1], [_zdiff])

        return score

    @staticmethod
    def _decrement(keep_candidates, score, keep_c, alphas, fuzzy, data) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """

        :return:
        """
        sco0 = score[0][keep_candidates]
        sco1 = score[1][keep_candidates]

        score = np.asarray([sco0, sco1])

        alphas = alphas[keep_c]
        fuzzy = fuzzy[keep_c]
        data = data[keep_c]

        return score, alphas, fuzzy, data

    @staticmethod
    def _filter_gradients(weights: np.ndarray, gradients: np.ndarray, data:
                          np.ndarray, label: np.ndarray) -> Tuple[Optional[np.ndarray], Optional[np.ndarray]]:
        """
        Filters data points based on the projected gradients.

        Kept data will include only values for which the projected gradients that will expand the support
        vectors, meaning that are outside boundaries of current support vectors of the classifier.

        :param gradients: The gradients with which to perform the computation
        :param data: Data to filter
        :return: Filtered data
        """
        _data = np.append(data, np.ones((len(data), 1)), axis=1)
        _new_grads = np.matmul(-_data, weights) - 1

        keep = np.where(np.logical_and(_new_grads >= min(gradients), _new_grads <= max(gradients)))

        index = keep[0]

        if not len(index):
            return None, None

        _data = data[index]
        _label = label[index]

        return _data, _label

    @classmethod
    def _fit_dag_step(cls, subset: TrainingSet, parameters: Hyperparameters) -> ClassificationModel:
        """
        Trains a classifier based on a sub-set of data, as a step in the DAG classifier algorithm.

        :param subset: Sub-set of data containing the training data for this DAG step
        :param parameters: The classifier hyperparameters
        :returns: A classification model for this subset
        """
        # Features (x_p) of the current "positive" class
        x_p = subset[0]
        y_p = subset[1]

        # Features (x_n) of the current "negative" class
        x_n = subset[2]
        y_n = subset[3]

        # Calculate fuzzy membership for points
        membership: FuzzyMembership = fuzzy_membership(params=parameters, class_p=x_p, class_n=x_n)

        # Build H matrix which is [X_p/n, e] where "e" is an extra column of ones ("1") appended at the end of the
        # matrix
        # i.e.
        #
        #   if  X_p = | 1  2  3 |  and   e = | 1 |  then  H_p = | 1 2 3 1 |
        #             | 4  5  6 |            | 1 |              | 4 5 6 1 |
        #             | 7  8  9 |            | 1 |              | 7 8 9 1 |
        #
        H_p = np.append(x_p, np.ones((x_p.shape[0], 1)), axis=1)
        H_n = np.append(x_n, np.ones((x_n.shape[0], 1)), axis=1)

        _C1 = parameters.C1 * membership.sn
        _C3 = parameters.C3 * membership.sp

        _C2 = parameters.C2
        _C4 = parameters.C4

        # Train the model using the algorithm described by (de Mello et al. 2019)

        # Python
        hyperplane_p: Hyperplane = train_model(parameters=parameters, H=H_n, G=H_p, C=_C4, CCx=_C3)
        hyperplane_n: Hyperplane = train_model(parameters=parameters, H=H_p, G=H_n, C=_C2, CCx=_C1)

        # Cython
        # _train_p = ctrain_model(parameters.max_iter, parameters.epsilon, H_n, H_p, _C4, _C3)
        # hyperplane_p = Hyperplane(*_train_p)
        # _train_n = ctrain_model(parameters.max_iter, parameters.epsilon, H_p, H_n, _C2, _C1)
        # hyperplane_n = Hyperplane(*_train_n)

        # Common to both Cython and Python
        hyperplane_n.weights = -hyperplane_n.weights

        return ClassificationModel(class_p=y_p[0],
                                   class_n=y_n[0],
                                   fuzzy=membership,
                                   weights_p=hyperplane_p,
                                   weights_n=hyperplane_n,
                                   data_p=x_p,
                                   data_n=x_n)

    @classmethod
    def _increment_dag_step(cls, subset: TrainingSet, parameters: Hyperparameters,
                            classifier: ClassificationModel) -> ClassificationModel:
        """
        Increment already trained DAG models

        :param subset: Sub-set of data containing the update data for this DAG step
        :param parameters: The classifier hyperparameters
        :param classifier: The classifier to update
        :return: The updated classifier
        """
        # Features (x_p) of the current "positive" class
        x_p = subset[0]
        y_p = subset[1]

        # Features (x_n) of the current "negative" class
        x_n = subset[2]
        y_n = subset[3]

        score_p = None
        score_n = None

        _batch_xp, _batch_yp = cls._filter_gradients(weights=classifier.p.weights,
                                                     gradients=classifier.p.projected_gradients,
                                                     data=x_p, label=y_p)

        _batch_xn, _batch_yn = None, None
        if x_n.any() and y_n.any():
            _batch_xn, _batch_yn = cls._filter_gradients(weights=classifier.p.weights,
                                                         gradients=classifier.p.projected_gradients,
                                                         data=x_n, label=y_n)


        _data_xp = classifier.data_p
        if _batch_xp is not None and _batch_xp.any():
            _data_xp = np.concatenate((_data_xp, _batch_xp)) if _batch_xp is not None else classifier.data_p

        _data_xn = classifier.data_n
        if _batch_xn is not None and _batch_xn.any():
            _data_xn = np.concatenate((_data_xn, _batch_xn)) if _batch_xn is not None else classifier.data_n

        # Calculate fuzzy membership for points
        membership: FuzzyMembership = fuzzy_membership(params=parameters, class_p=_data_xp, class_n=_data_xn)

        # Build H matrix which is [X_p/n, e] where "e" is an extra column of ones ("1") appended at the end of the
        # matrix
        # i.e.
        #
        #   if  X_p = | 1  2  3 |  and   e = | 1 |  then  H_p = | 1 2 3 1 |
        #             | 4  5  6 |            | 1 |              | 4 5 6 1 |
        #             | 7  8  9 |            | 1 |              | 7 8 9 1 |
        #
        H_p = np.append(_data_xp, np.ones((_data_xp.shape[0], 1)), axis=1)
        H_n = np.append(_data_xn, np.ones((_data_xn.shape[0], 1)), axis=1)

        _C1 = parameters.C1 * membership.sn
        _C3 = parameters.C3 * membership.sp

        _C2 = parameters.C2
        _C4 = parameters.C4

        # Recompute the training with the update data
        hyperplane_p: Hyperplane = train_model(parameters=parameters, H=H_n, G=H_p, C=_C4, CCx=_C3)
        hyperplane_n: Hyperplane = train_model(parameters=parameters, H=H_p, G=H_n, C=_C2, CCx=_C1)
        hyperplane_n.weights = -hyperplane_n.weights

        classifier.p = hyperplane_p
        classifier.n = hyperplane_n
        classifier.fuzzy_membership = membership

        c_pos = np.nonzero(classifier.p.alpha <= parameters.phi)[0]
        c_neg = np.nonzero(classifier.n.alpha <= parameters.phi)[0]
        keep_c_pos = np.nonzero(classifier.p.alpha > parameters.phi)[0]
        keep_c_neg = np.nonzero(classifier.n.alpha > parameters.phi)[0]

        score_p = cls._compute_score(score_p, c_pos)
        score_n = cls._compute_score(score_n, c_neg)

        _keep_candidates_p = np.where(score_p[1] < parameters.repetition)[0]
        _keep_candidates_n = np.where(score_n[1] < parameters.repetition)[0]

        if _keep_candidates_p.any():

            score, alpha, fuzzy, data = cls._decrement(keep_candidates=_keep_candidates_p,
                                                       score=score_p,
                                                       keep_c=keep_c_pos,
                                                       alphas=classifier.p.alpha,
                                                       fuzzy=classifier.fuzzy_membership.sp,
                                                       data=_data_xp)

            classifier.p.alpha = alpha
            classifier.fuzzy_membership.sp = fuzzy
            classifier.data_p = data

        if _keep_candidates_n.any():
            score, alpha, fuzzy, data = cls._decrement(keep_candidates=_keep_candidates_n,
                                                       score=score_n,
                                                       keep_c=keep_c_neg,
                                                       alphas=classifier.n.alpha,
                                                       fuzzy=classifier.fuzzy_membership.sn,
                                                       data=_data_xn)

            classifier.n.alpha = alpha
            classifier.fuzzy_membership.sn = fuzzy
            classifier.data_n = data

        return classifier

    @classmethod
    def _generate_sub_sets(cls, X: np.ndarray, y: np.ndarray) -> DAGSubSet:
        """
        Generates sub-data sets based on the DAG classification principle.

        Example, for 4 classes, the function will return the following:
        [0]: Values and labels of Class 1 and 2
        [1]: Values and labels of Class 1 and 3
        [2]: Values and labels of Class 1 and 4
        [3]: Values and labels of Class 2 and 3
        [4]: Values and labels of Class 2 and 4
        [5]: Values and labels of Class 3 and 4

        :param X: The full training set
        :param y: The full training labels set
        :return: Generator of tuple containing values and labels for positive and negative class
                 based on the current iteration in the classification DAG.

        - [0] Values for current X positive
        - [1] Labels for current X positive
        - [2] Values for current X negative
        - [3] Labels for current X negative
        """
        classes = np.unique(y)
        if len(classes) == 1:
            return X[classes[0]], y[classes[0]], np.ndarray(), np.ndarray()

        for _p in range(classes.size):

            for _n in range(_p + 1, classes.size):
                _index_p = np.where(y == classes[_p])[0]
                _index_n = np.where(y == classes[_n])[0]

                yield X[_index_p], y[_index_p], X[_index_n], y[_index_n]

    def decision_function(self, X):
        """
        Evalutes the decision function over X.

        :param X: Array of features to evaluate the decision on.
        :return: Array of decision evaluation.
        """
        pass

    def fit(self, X: np.ndarray, y: np.ndarray, sample_weight=None):
        """
        Trains a iFBTSVM model

        :param X: The training samples
        :param y: The class labels for each training sample

        :param sample_weight: (Not supported)
        """

        X = self.kernel.fit_transform(X=X, y=y) if self.kernel else X  # type: ignore

        # Train the DAG models in parallel
        trained_hyperplanes = Parallel(n_jobs=self.n_jobs, prefer='processes')(
            delayed(self._fit_dag_step)(subset, self.parameters) for subset in self._generate_sub_sets(X, y)
        )

        # Create the DAG Model here
        for hypp in trained_hyperplanes:
            _clsf = self._classifiers.get(hypp.class_p, {})
            _clsf[hypp.class_n] = hypp
            self._classifiers[hypp.class_p] = _clsf

    def get_params(self, deep=True):
        """
        Returns parameters of the classifier.

        :param deep: Not-implemented, as no effect and is kept to comply with sklearn.svm.SVC interface
        :return: A dictionary of parameters
        """
        return self.parameters

    def update(self, X: np.ndarray, y: np.ndarray, batch_size: int = None):
        """
        Update an already trained classifier

        :param X: The training data with which to update the models.
        :param y: The training labels with which to update the models.
        :param batch_size: The batch size for updating models
        """
        if not batch_size:
            batch_size = len(y)

        i = 0
        while i < len(X):

            batch_x = X[i: i + batch_size]
            batch_y = y[i: i + batch_size]

            batch_x = self.kernel.fit_transform(X=batch_x, y=batch_y) if self.kernel else batch_x  # type: ignore

            # Update the DAG models in parallel
            updated_hyperplanes = Parallel(n_jobs=self.n_jobs, prefer='processes')(
                delayed(self._increment_dag_step)
                (
                    subset,
                    self.parameters,
                    self._classifiers[subset[1][0]][subset[3][0]]  # Get classifier for ClassP/ClassN of this subset
                )
                for subset in self._generate_sub_sets(batch_x, batch_y)
            )

            # Create the DAG Model here
            for hypp in updated_hyperplanes:
                _clsf = self._classifiers.get(hypp.class_p, {})
                _clsf[hypp.class_n] = hypp
                self._classifiers[hypp.class_p] = _clsf

            i += batch_size

    def predict(self, X):
        """
        Performs classification X.

        :param X: Array of features to classify
        :return: Array of classification result
        """
        X = self.kernel.transform(X=X) if self.kernel else X

        lh_keys = list(set(self._classifiers.keys()))

        rh_keys = set()
        for value in self._classifiers.values():
            for key, _ in value.items():
                rh_keys.add(key)
        rh_keys = list(rh_keys)

        classes = []

        for row in X:

            _dag_index_p = 0
            _dag_index_n = 0

            f_pos = 0
            f_neg = 0

            class_p = None
            class_n = None

            while True:
                try:
                    class_p = lh_keys[_dag_index_p]
                    class_n = rh_keys[_dag_index_n]

                    model: ClassificationModel = self._classifiers[class_p][class_n]

                    f_pos = np.divide(np.matmul(row, model.p.weights[:-1]) + model.p.weights[-1],
                                      linalg.norm(model.p.weights[:-1]))
                    f_neg = np.divide(np.matmul(row, model.n.weights[:-1]) + model.n.weights[-1],
                                      linalg.norm(model.n.weights[:-1]))

                    if abs(f_pos) < abs(f_neg):
                        _dag_index_p = _dag_index_n + 1
                        _dag_index_n += 1

                    else:
                        _dag_index_n += 1

                except (StopIteration, IndexError):

                    if abs(f_pos) < abs(f_neg):
                        classes.append(class_n)

                    else:
                        classes.append(class_p)

                    break

        return classes

    def set_params(self, **params):
        """
        Sets parameters of the classifier

        :param params: Keyword arguments and their values
        :return: None
        """
        for key, val in params.items():
            setattr(self.parameters, key, val)

    def score(self, X, y, sample_weight=None):
        """
        Returns the accuracy of a classification.

        :param X: Array of features to classify
        :param y: 1-D Array of truth values for the features
        :param sample_weight: Not supported
        :return: Accuracy score of the classification
        """
        return accuracy_score(y, self.predict(X), sample_weight=sample_weight)
