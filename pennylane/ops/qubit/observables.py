# Copyright 2018-2021 Xanadu Quantum Technologies Inc.

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
This submodule contains the discrete-variable quantum observables,
excepting the Pauli gates and Hadamard gate in ``non_parametric_ops.py``.
"""

import warnings
from collections.abc import Sequence
from copy import copy

import numpy as np
from scipy.sparse import csr_matrix, spmatrix

import pennylane as qml
from pennylane.operation import Operation, Operator
from pennylane.typing import TensorLike
from pennylane.wires import Wires, WiresLike

from .matrix_ops import QubitUnitary


class Hermitian(Operator):
    r"""
    An arbitrary Hermitian observable.

    For a Hermitian matrix :math:`A`, the expectation command returns the value

    .. math::
        \braket{A} = \braketT{\psi}{\cdots \otimes I\otimes A\otimes I\cdots}{\psi}

    where :math:`A` acts on the requested wires.

    If acting on :math:`N` wires, then the matrix :math:`A` must be of size
    :math:`2^N\times 2^N`.

    **Details:**

    * Number of wires: Any
    * Number of parameters: 1
    * Gradient recipe: None

    Args:
        A (array or Sequence): square hermitian matrix
        wires (Sequence[int] or int): the wire(s) the operation acts on
        id (str or None): String representing the operation (optional)
    """

    _queue_category = None

    is_hermitian = True
    num_params = 1
    """int: Number of trainable parameters that the operator depends on."""

    ndim_params = (2,)
    """tuple[int]: Number of dimensions per trainable parameter that the operator depends on."""

    grad_method = "F"

    # Qubit case
    _num_basis_states = 2
    _eigs = {}

    def __init__(self, A: TensorLike, wires: WiresLike, id: str | None = None):
        A = np.array(A) if isinstance(A, list) else A
        if not qml.math.is_abstract(A):
            if isinstance(wires, Sequence) and not isinstance(wires, str):
                if len(wires) == 0:
                    raise ValueError(
                        "Hermitian: wrong number of wires. At least one wire has to be given."
                    )
                expected_mx_shape = self._num_basis_states ** len(wires)
            else:
                # Assumably wires is an int; further validation checks are performed by calling super().__init__
                expected_mx_shape = self._num_basis_states

            Hermitian._validate_input(A, expected_mx_shape)

        super().__init__(A, wires=wires, id=id)

    @staticmethod
    def _validate_input(A: TensorLike, expected_mx_shape: int | None = None):
        """Validate the input matrix."""
        if len(A.shape) != 2 or A.shape[0] != A.shape[1]:
            raise ValueError("Observable must be a square matrix.")

        if expected_mx_shape is not None and A.shape[0] != expected_mx_shape:
            raise ValueError(
                f"Expected input matrix to have shape {expected_mx_shape}x{expected_mx_shape}, but "
                f"a matrix with shape {A.shape[0]}x{A.shape[0]} was passed."
            )

    def label(
        self,
        decimals: int | None = None,
        base_label: str | None = None,
        cache: dict | None = None,
    ) -> str:
        return super().label(decimals=decimals, base_label=base_label or "𝓗", cache=cache)

    @staticmethod
    def compute_matrix(A: TensorLike) -> TensorLike:  # pylint: disable=arguments-differ
        r"""Representation of the operator as a canonical matrix in the computational basis (static method).

        The canonical matrix is the textbook matrix representation that does not consider wires.
        Implicitly, this assumes that the wires of the operator correspond to the global wire order.

        .. seealso:: :meth:`~.Hermitian.matrix`

        Args:
            A (tensor_like): hermitian matrix

        Returns:
            tensor_like: canonical matrix

        **Example**

        >>> A = np.array([[6+0j, 1-2j],[1+2j, -1]])
        >>> qml.Hermitian.compute_matrix(A)
        [[ 6.+0.j  1.-2.j]
         [ 1.+2.j -1.+0.j]]
        """
        A = qml.math.asarray(A)
        Hermitian._validate_input(A)
        return A

    # pylint: disable=arguments-differ
    @staticmethod
    def compute_sparse_matrix(A, format="csr") -> csr_matrix:
        return csr_matrix(Hermitian.compute_matrix(A)).asformat(format)

    @property
    def eigendecomposition(self) -> dict[str, TensorLike]:
        """Return the eigendecomposition of the matrix specified by the Hermitian observable.

        This method uses pre-stored eigenvalues for standard observables where
        possible and stores the corresponding eigenvectors from the eigendecomposition.

        It transforms the input operator according to the wires specified.

        Returns:
            dict[str, array]: dictionary containing the eigenvalues and the eigenvectors of the Hermitian observable
        """
        Hmat = self.matrix()
        Hmat = qml.math.to_numpy(Hmat)
        Hkey = tuple(Hmat.flatten().tolist())
        if Hkey not in Hermitian._eigs:
            w, U = np.linalg.eigh(Hmat)
            Hermitian._eigs[Hkey] = {"eigvec": U, "eigval": w}

        return Hermitian._eigs[Hkey]

    def eigvals(self) -> TensorLike:
        """Return the eigenvalues of the specified Hermitian observable.

        This method uses pre-stored eigenvalues for standard observables where
        possible and stores the corresponding eigenvectors from the eigendecomposition.

        Returns:
            array: array containing the eigenvalues of the Hermitian observable
        """
        return self.eigendecomposition["eigval"]

    @staticmethod
    def compute_decomposition(A, wires):  # pylint: disable=arguments-differ
        r"""Decomposes a hermitian matrix as a sum of Pauli operators.

        Args:
            A (array or Sequence): hermitian matrix
            wires (Iterable[Any], Wires): wires that the operator acts on
        Returns:
            list[.Operator]: decomposition of the hermitian matrix

        **Examples**

        >>> op = qml.X(0) + qml.Y(1) + 2 * qml.X(0) @ qml.Z(3)
        >>> op_matrix = qml.matrix(op)
        >>> qml.Hermitian.compute_decomposition(op_matrix, wires=['a', 'b', 'aux'])
        [(
              1.0 * (I('a') @ Y('b') @ I('aux'))
            + 1.0 * (X('a') @ I('b') @ I('aux'))
            + 2.0 * (X('a') @ I('b') @ Z('aux'))
        )]
        >>> op = np.array([[1, 1], [1, -1]]) / np.sqrt(2)
        >>> qml.Hermitian.compute_decomposition(op, wires=0)
        [(
              0.7071067811865475 * X(0)
            + 0.7071067811865475 * Z(0)
        )]

        """
        A = qml.math.asarray(A)

        if isinstance(wires, (int, str)):
            wires = Wires(wires)

        if len(wires) == 0:
            raise ValueError("Hermitian: wrong number of wires. At least one wire has to be given.")
        Hermitian._validate_input(A, expected_mx_shape=2 ** len(wires))

        # determined heuristically from test_hermitian_decomposition_performance
        if len(wires) > 7:
            warnings.warn(
                "Decomposition may be inefficient for this large of a matrix.",
                UserWarning,
            )

        return [qml.pauli.conversion.pauli_decompose(A, wire_order=wires, pauli=False)]

    @staticmethod
    def compute_diagonalizing_gates(  # pylint: disable=arguments-differ
        eigenvectors: TensorLike, wires: WiresLike
    ) -> list["qml.operation.Operator"]:
        r"""Sequence of gates that diagonalize the operator in the computational basis (static method).

        Given the eigendecomposition :math:`O = U \Sigma U^{\dagger}` where
        :math:`\Sigma` is a diagonal matrix containing the eigenvalues,
        the sequence of diagonalizing gates implements the unitary :math:`U^{\dagger}`.

        The diagonalizing gates rotate the state into the eigenbasis
        of the operator.

        .. seealso:: :meth:`~.Hermitian.diagonalizing_gates`.

        Args:
            eigenvectors (array): eigenvectors of the operator, as extracted from op.eigendecomposition["eigvec"].
            wires (Iterable[Any], Wires): wires that the operator acts on
        Returns:
            list[.Operator]: list of diagonalizing gates

        **Example**

        >>> A = np.array([[-6, 2 + 1j], [2 - 1j, 0]])
        >>> _, evecs = np.linalg.eigh(A)
        >>> qml.Hermitian.compute_diagonalizing_gates(evecs, wires=[0])
        [QubitUnitary(tensor([[-0.94915323-0.j,  0.2815786 +0.1407893j ],
                              [ 0.31481445-0.j,  0.84894846+0.42447423j]], requires_grad=True), wires=[0])]

        """
        return [QubitUnitary(eigenvectors.conj().T, wires=wires)]

    def diagonalizing_gates(self) -> list["qml.operation.Operator"]:
        """Return the gate set that diagonalizes a circuit according to the
        specified Hermitian observable.

        Returns:
            list: list containing the gates diagonalizing the Hermitian observable
        """
        # note: compute_diagonalizing_gates has a custom signature, which is why we overwrite this method
        return self.compute_diagonalizing_gates(self.eigendecomposition["eigvec"], self.wires)


class SparseHamiltonian(Operator):
    r"""
    A Hamiltonian represented directly as a sparse matrix in Compressed Sparse Row (CSR) format.

    .. warning::

        ``SparseHamiltonian`` observables can only be used to return expectation values.
        Variances and samples are not supported.

    **Details:**

    * Number of wires: Any
    * Number of parameters: 1
    * Gradient recipe: None

    Args:
        H (csr_matrix): a sparse matrix in SciPy Compressed Sparse Row (CSR) format with
            dimension :math:`(2^n, 2^n)`, where :math:`n` is the number of wires.
        wires (Sequence[int]): the wire(s) the operation acts on
        id (str or None): String representing the operation (optional)

    **Example**

    Sparse Hamiltonians can be constructed directly with a SciPy-compatible sparse matrix.

    Alternatively, you can construct your Hamiltonian as usual using :class:`~.LinearCombination`, and then use
    :meth:`~.LinearCombination.sparse_matrix` to construct the sparse matrix that serves as the input
    to ``SparseHamiltonian``:

    >>> wires = range(20)
    >>> coeffs = [1 for _ in wires]
    >>> observables = [qml.Z(i) for i in wires]
    >>> H = qml.Hamiltonian(coeffs, observables)
    >>> Hmat = H.sparse_matrix()
    >>> H_sparse = qml.SparseHamiltonian(Hmat, wires)
    """

    _queue_category = None
    is_hermitian = True
    num_params = 1
    """int: Number of trainable parameters that the operator depends on."""

    grad_method = None

    def __init__(self, H: csr_matrix, wires: WiresLike, id: str | None = None):
        if not isinstance(H, csr_matrix):
            raise TypeError("Observable must be a scipy sparse csr_matrix.")
        super().__init__(H, wires=wires, id=id)
        self.H = H
        mat_len = 2 ** len(self.wires)
        if H.shape != (mat_len, mat_len):
            raise ValueError(
                f"Sparse Matrix must be of shape ({mat_len}, {mat_len}). Got {H.shape}."
            )

    def __mul__(self, value: int | float) -> "qml.SparseHamiltonian":
        r"""The scalar multiplication operation between a scalar and a SparseHamiltonian."""
        if not isinstance(value, (int, float)) and qml.math.ndim(value) != 0:
            raise TypeError(f"Scalar value must be an int or float. Got {type(value)}")

        return qml.SparseHamiltonian(csr_matrix.multiply(self.H, value), wires=self.wires)

    __rmul__ = __mul__

    def label(
        self,
        decimals: int | None = None,
        base_label: str | None = None,
        cache: dict | None = None,
    ) -> str:
        return super().label(decimals=decimals, base_label=base_label or "𝓗", cache=cache)

    @staticmethod
    def compute_matrix(H: csr_matrix) -> np.ndarray:  # pylint: disable=arguments-differ
        r"""Representation of the operator as a canonical matrix in the computational basis (static method).

        The canonical matrix is the textbook matrix representation that does not consider wires.
        Implicitly, this assumes that the wires of the operator correspond to the global wire order.

        .. seealso:: :meth:`~.SparseHamiltonian.matrix`


        This method returns a dense matrix. For a sparse matrix representation, see
        :meth:`~.SparseHamiltonian.compute_sparse_matrix`.

        Args:
            H (scipy.sparse.csr_matrix): sparse matrix used to create the operator

        Returns:
            array: dense matrix

        **Example**

        >>> from scipy.sparse import csr_matrix
        >>> H = np.array([[6+0j, 1-2j],[1+2j, -1]])
        >>> H = csr_matrix(H)
        >>> res = qml.SparseHamiltonian.compute_matrix(H)
        >>> res
        [[ 6.+0.j  1.-2.j]
         [ 1.+2.j -1.+0.j]]
        >>> type(res)
        <class 'numpy.ndarray'>
        """
        return H.toarray()

    # pylint: disable=arguments-differ
    # TODO: Remove when PL supports pylint==3.3.6 (it is considered a useless-suppression) [sc-91362]
    # pylint: disable=unused-argument
    @staticmethod
    def compute_sparse_matrix(H: spmatrix, format="csr") -> spmatrix:
        r"""Representation of the operator as a sparse canonical matrix in the computational basis (static method).

        The canonical matrix is the textbook matrix representation that does not consider wires.
        Implicitly, this assumes that the wires of the operator correspond to the global wire order.

        .. seealso:: :meth:`~.SparseHamiltonian.sparse_matrix`

        This method returns a sparse matrix. For a dense matrix representation, see
        :meth:`~.SparseHamiltonian.compute_matrix`.

        Args:
            H (scipy.sparse.csr_matrix): sparse matrix used to create the operator

        Returns:
            scipy.sparse.csr_matrix: sparse matrix

        **Example**

        >>> from scipy.sparse import csr_matrix
        >>> H = np.array([[6+0j, 1-2j],[1+2j, -1]])
        >>> H = csr_matrix(H)
        >>> res = qml.SparseHamiltonian.compute_sparse_matrix(H)
        >>> res
        (0, 0)	(6+0j)
        (0, 1)	(1-2j)
        (1, 0)	(1+2j)
        (1, 1)	(-1+0j)
        >>> type(res)
        <class 'scipy.sparse.csr_matrix'>
        """
        return H


class Projector(Operator):
    r"""Projector(state, wires, id=None)
    Observable corresponding to the state projector :math:`P=\ket{\phi}\bra{\phi}`.

    The expectation of this observable returns the value

    .. math::
        |\langle \psi | \phi \rangle |^2

    corresponding to the probability that :math:`|\psi\rangle` is projected onto :math:`|\phi\rangle` during measurement.

    **Details:**

    * Number of wires: Any
    * Number of parameters: 1
    * Gradient recipe: None

    Args:
        state (tensor-like): Input state of shape ``(n,)`` for a basis-state projector, or ``(2**n,)``
            for a statevector projector.
        wires (Iterable): wires that the projector acts on.
        id (str or None): String representing the operation (optional).

    **Example**

    In the following example we consider projectors over two states: the :math:`|00\rangle` and the
    :math:`|++\rangle`. Since the first one is in the computational basis, we create its projector
    directly from its basis state representation, which is, ``zero_state=[0, 0]``. For the latter,
    we need to use its state vector form ``plusplus_state=np.array([1, 1, 1, 1])/2``.

    .. code-block::

        >>> dev = qml.device("default.qubit", wires=2)
        >>> @qml.qnode(dev)
        ... def circuit(state):
        ...     return qml.expval(qml.Projector(state, wires=[0, 1]))
        >>> zero_state = [0, 0]
        >>> circuit(zero_state)
        1.
        >>> plusplus_state = np.array([1, 1, 1, 1]) / 2
        >>> circuit(plusplus_state)
        0.25

    """

    is_hermitian = True
    name = "Projector"
    num_params = 1
    _queue_category = None
    """int: Number of trainable parameters that the operator depends on."""

    ndim_params = (1,)
    """tuple[int]: Number of dimensions per trainable parameter that the operator depends on."""

    def __new__(cls, state: TensorLike, wires: WiresLike, **_):
        """Changes parents based on the state representation.

        Though all the types will be named "Projector", their *identity* and location in memory
        will be different based on whether the input state is a basis state or a state vector.
        We cache the different types in private class variables so that:

        >>> Projector(state, wires).__class__ is Projector(state, wires).__class__
        True
        >>> type(Projector(state, wries)) == type(Projector(state, wires))
        True
        >>> isinstance(Projector(state, wires), type(Projector(state, wires)))
        True
        >>> Projector(basis_state, wires).__class__ is Projector._basis_state_type
        True
        >>> Projector(state_vector, wires).__class__ is Projector._state_vector_type
        True

        """
        wires = Wires(wires)
        shape = qml.math.shape(state)
        if len(shape) != 1:
            raise ValueError(f"Input state must be one-dimensional; got shape {shape}.")

        if len(state) == len(wires):
            return object.__new__(BasisStateProjector)

        if len(state) == 2 ** len(wires):
            return object.__new__(StateVectorProjector)

        raise ValueError(
            "Input state should have the same length as the wires in the case "
            "of a basis state, or 2**len(wires) in case of a state vector. "
            f"The lengths for the input state and wires are {len(state)} and "
            f"{len(wires)}, respectively."
        )

    def pow(self, z: int | float) -> list["qml.operation.Operator"]:
        """Raise this projector to the power ``z``."""
        return [copy(self)] if (isinstance(z, int) and z > 0) else super().pow(z)


class BasisStateProjector(Projector, Operation):
    r"""Observable corresponding to the state projector :math:`P=\ket{\phi}\bra{\phi}`, where
    :math:`\phi` denotes a basis state."""

    grad_method = None
    _queue_category = "_ops"

    # The call signature should be the same as Projector.__new__ for the positional
    # arguments, but with free key word arguments.
    def __init__(self, state: TensorLike, wires: WiresLike, id: str | None = None):
        wires = Wires(wires)

        if qml.math.get_interface(state) == "jax":
            dtype = qml.math.dtype(state)
            if not (np.issubdtype(dtype, np.integer) or np.issubdtype(dtype, bool)):
                raise ValueError("Basis state must consist of integers or booleans.")
        else:
            # state is index into data, rather than data, so cast it to built-ins when
            # no need for tracing
            state = tuple(qml.math.toarray(state).astype(int))
            if not set(state).issubset({0, 1}):
                raise ValueError(f"Basis state must only consist of 0s and 1s; got {state}")

        super().__init__(state, wires=wires, id=id)

    def __new__(cls, *_, **__):
        return object.__new__(cls)

    def label(
        self,
        decimals: int | None = None,
        base_label: str | None = None,
        cache: dict | None = None,
    ) -> str:
        r"""A customizable string representation of the operator.

        Args:
            decimals=None (int): If ``None``, no parameters are included. Else,
                specifies how to round the parameters.
            base_label=None (str): overwrite the non-parameter component of the label.
            cache=None (dict): dictionary that caries information between label calls
                in the same drawing.

        Returns:
            str: label to use in drawings.

        **Example:**

        >>> BasisStateProjector([0, 1, 0], wires=(0, 1, 2)).label()
        '|010⟩⟨010|'

        """

        if base_label is not None:
            return base_label
        basis_string = "".join(str(int(i)) for i in self.data[0])
        return f"|{basis_string}⟩⟨{basis_string}|"

    @staticmethod
    def compute_matrix(basis_state: TensorLike) -> np.ndarray:  # pylint: disable=arguments-differ
        r"""Representation of the operator as a canonical matrix in the computational basis (static method).

        The canonical matrix is the textbook matrix representation that does not consider wires.
        Implicitly, this assumes that the wires of the operator correspond to the global wire order.

        .. seealso:: :meth:`~.BasisStateProjector.matrix`

        Args:
            basis_state (Iterable): basis state to project on

        Returns:
            ndarray: matrix

        **Example**

        >>> BasisStateProjector.compute_matrix([0, 1])
        [[0. 0. 0. 0.]
         [0. 1. 0. 0.]
         [0. 0. 0. 0.]
         [0. 0. 0. 0.]]
        """
        shape = (2 ** len(basis_state), 2 ** len(basis_state))
        if qml.math.get_interface(basis_state) == "jax":
            idx = 0
            for i, m in enumerate(basis_state):
                idx = idx + (m << (len(basis_state) - i - 1))
            mat = qml.math.zeros(shape, like=basis_state)
            return mat.at[idx, idx].set(1.0)

        m = np.zeros(shape)
        idx = int("".join(str(i) for i in basis_state), 2)
        m[idx, idx] = 1
        return m

    @staticmethod
    def compute_eigvals(basis_state: TensorLike) -> np.ndarray:  # pylint: disable=arguments-differ
        r"""Eigenvalues of the operator in the computational basis (static method).

        If :attr:`diagonalizing_gates` are specified and implement a unitary :math:`U^{\dagger}`,
        the operator can be reconstructed as

        .. math:: O = U \Sigma U^{\dagger},

        where :math:`\Sigma` is the diagonal matrix containing the eigenvalues.

        Otherwise, no particular order for the eigenvalues is guaranteed.

        .. seealso:: :meth:`~.BasisStateProjector.eigvals`

        Args:
            basis_state (Iterable): basis state to project on

        Returns:
            array: eigenvalues

        **Example**

        >>> BasisStateProjector.compute_eigvals([0, 1])
        [0. 1. 0. 0.]
        """
        if qml.math.get_interface(basis_state) == "jax":
            idx = 0
            for i, m in enumerate(basis_state):
                idx = idx + (m << (len(basis_state) - i - 1))
            eigvals = qml.math.zeros(2 ** len(basis_state), like=basis_state)
            return eigvals.at[idx].set(1.0)
        w = np.zeros(2 ** len(basis_state))
        idx = int("".join(str(i) for i in basis_state), 2)
        w[idx] = 1
        return w

    @staticmethod
    def compute_diagonalizing_gates(  # pylint: disable=arguments-differ,unused-argument
        basis_state: TensorLike,
        wires: WiresLike,
    ) -> list["qml.operation.Operator"]:
        r"""Sequence of gates that diagonalize the operator in the computational basis (static method).

        Given the eigendecomposition :math:`O = U \Sigma U^{\dagger}` where
        :math:`\Sigma` is a diagonal matrix containing the eigenvalues,
        the sequence of diagonalizing gates implements the unitary :math:`U^{\dagger}`.

        The diagonalizing gates rotate the state into the eigenbasis
        of the operator.

        .. seealso:: :meth:`~.BasisStateProjector.diagonalizing_gates`.

        Args:
            basis_state (Iterable): basis state that the operator projects on
            wires (Iterable[Any], Wires): wires that the operator acts on
        Returns:
            list[.Operator]: list of diagonalizing gates

        **Example**

        >>> BasisStateProjector.compute_diagonalizing_gates([0, 1, 0, 0], wires=[0, 1])
        []
        """
        return []

    @staticmethod
    def compute_sparse_matrix(  # pylint: disable=arguments-differ
        basis_state: TensorLike, format="csr"
    ) -> spmatrix:
        """
        Computes the sparse CSR matrix representation of the projector onto the basis state.

        Args:
            basis_state (Iterable): The basis state as an iterable of integers (0 or 1).

        Returns:
            scipy.sparse.csr_matrix: The sparse CSR matrix representation of the projector.
        """

        num_qubits = len(basis_state)
        data = [1]
        rows = [int("".join(str(bit) for bit in basis_state), 2)]
        cols = rows
        return csr_matrix((data, (rows, cols)), shape=(2**num_qubits, 2**num_qubits)).asformat(
            format
        )


class StateVectorProjector(Projector):
    r"""Observable corresponding to the state projector :math:`P=\ket{\phi}\bra{\phi}`, where
    :math:`\phi` denotes a state."""

    grad_method = None

    # The call signature should be the same as Projector.__new__ for the positional
    # arguments, but with free key word arguments.
    def __init__(self, state: TensorLike, wires: WiresLike, id: str | None = None):
        wires = Wires(wires)
        super().__init__(state, wires=wires, id=id)

    def __new__(cls, *_, **__):
        return object.__new__(cls)

    # pylint: disable=unused-argument
    def label(
        self,
        decimals: int | None = None,
        base_label: str | None = None,
        cache: dict | None = None,
    ) -> str:
        r"""A customizable string representation of the operator.

        Args:
            decimals=None (int): If ``None``, no parameters are included. Else,
                specifies how to round the parameters.
            base_label=None (str): overwrite the non-parameter component of the label.
            cache=None (dict): dictionary that caries information between label calls
                in the same drawing.

        Returns:
            str: label to use in drawings.

        **Example:**

        >>> state_vector = np.array([0, 1, 1, 0])/np.sqrt(2)
        >>> qml.Projector(state_vector, wires=(0, 1)).label()
        'P'
        >>> qml.Projector(state_vector, wires=(0, 1)).label(base_label="hi!")
        'hi!'
        >>> dev = qml.device("default.qubit", wires=1)
        >>> @qml.qnode(dev)
        >>> def circuit(state):
        ...     return qml.expval(qml.Projector(state, [0]))
        >>> print(qml.draw(circuit)([1, 0]))
        0: ───┤  <|0⟩⟨0|>
        >>> print(qml.draw(circuit)(np.array([1, 1]) / np.sqrt(2)))
        0: ───┤  <P(M0)>
        M0 =
        [0.70710678 0.70710678]

        """
        if base_label is not None:
            return base_label

        state_vector = self.parameters[0]
        n_wires = int(qml.math.log2(len(state_vector)))
        basis_state_idx = qml.math.nonzero(state_vector)[0]

        if len(basis_state_idx) == 1:
            basis_string = f"{basis_state_idx[0]:0{n_wires}b}"
            return f"|{basis_string}⟩⟨{basis_string}|"

        if cache is None or not isinstance(cache.get("matrices", None), list):
            return "P"

        mat_num = len(cache["matrices"])
        cache["matrices"].append(self.parameters[0])
        return f"P(M{mat_num})"

    @staticmethod
    def compute_matrix(  # pylint: disable=arguments-differ
        state_vector: TensorLike,
    ) -> np.ndarray:
        r"""Representation of the operator as a canonical matrix in the computational basis (static method).

        The canonical matrix is the textbook matrix representation that does not consider wires.
        Implicitly, this assumes that the wires of the operator correspond to the global wire order.

        .. seealso:: :meth:`~.Projector.matrix`

        Args:
            state_vector (Iterable): state vector to project on

        Returns:
            ndarray: matrix

        **Example**

        The projector of the state :math:`\frac{1}{\sqrt{2}}(\ket{01}+\ket{10})`

        >>> StateVectorProjector.compute_matrix([0, 1/np.sqrt(2), 1/np.sqrt(2), 0])
        [[0. 0.  0.  0.]
         [0. 0.5 0.5 0.]
         [0. 0.5 0.5 0.]
         [0. 0.  0.  0.]]
        """
        return qml.math.outer(state_vector, qml.math.conj(state_vector))

    @staticmethod
    def compute_eigvals(  # pylint: disable=arguments-differ
        state_vector: TensorLike,
    ) -> np.ndarray:
        r"""Eigenvalues of the operator in the computational basis (static method).

        If :attr:`diagonalizing_gates` are specified and implement a unitary :math:`U^{\dagger}`,
        the operator can be reconstructed as

        .. math:: O = U \Sigma U^{\dagger},

        where :math:`\Sigma` is the diagonal matrix containing the eigenvalues.

        Otherwise, no particular order for the eigenvalues is guaranteed.

        .. seealso:: :meth:`~.StateVectorProjector.eigvals`

        Args:
            state_vector (Iterable): state vector to project on

        Returns:
            array: eigenvalues

        **Example**

        >>> StateVectorProjector.compute_eigvals([0, 0, 1, 0])
        array([1, 0, 0, 0])
        """
        precision = qml.math.get_dtype_name(state_vector)[-2:]
        dtype = f"float{precision}" if precision in {"32", "64"} else "float64"
        w = np.zeros(qml.math.shape(state_vector), dtype=dtype)
        w[0] = 1
        return qml.math.convert_like(w, state_vector)

    @staticmethod
    def compute_diagonalizing_gates(  # pylint: disable=arguments-differ
        state_vector: TensorLike, wires: WiresLike
    ) -> list["qml.operation.Operator"]:
        r"""Sequence of gates that diagonalize the operator in the computational basis (static method).

        Given the eigendecomposition :math:`O = U \Sigma U^{\dagger}` where
        :math:`\Sigma` is a diagonal matrix containing the eigenvalues,
        the sequence of diagonalizing gates implements the unitary :math:`U^{\dagger}`.

        The diagonalizing gates rotate the state into the eigenbasis
        of the operator.

        .. seealso:: :meth:`~.StateVectorProjector.diagonalizing_gates`.

        Args:
            state_vector (Iterable): state vector that the operator projects on.
            wires (Iterable[Any], Wires): wires that the operator acts on.
        Returns:
            list[.Operator]: list of diagonalizing gates.

        **Example**

        >>> state_vector = np.array([1., 1j])/np.sqrt(2)
        >>> StateVectorProjector.compute_diagonalizing_gates(state_vector, wires=[0])
        [QubitUnitary(array([[ 0.70710678+0.j        ,  0.        -0.70710678j],
                             [ 0.        +0.70710678j, -0.70710678+0.j        ]]), wires=[0])]
        """
        # Adapting the approach discussed in the link below to work with arbitrary complex-valued state vectors.
        # Alternatively, we could take the adjoint of the Mottonen decomposition for the state vector.
        # https://quantumcomputing.stackexchange.com/questions/10239/how-can-i-fill-a-unitary-knowing-only-its-first-column

        if qml.math.get_interface(state_vector) == "tensorflow":
            dtype_name = qml.math.get_dtype_name(state_vector)
            if dtype_name == "int32":
                state_vector = qml.math.cast(state_vector, np.complex64)
            elif dtype_name == "int64":
                state_vector = qml.math.cast(state_vector, np.complex128)

        angle = qml.math.angle(state_vector[0])
        if qml.math.get_interface(angle) == "tensorflow":
            if qml.math.get_dtype_name(angle) == "float32":
                angle = qml.math.cast(angle, np.complex64)
            else:
                angle = qml.math.cast(angle, np.complex128)

        phase = qml.math.exp(-1.0j * angle)
        psi = phase * state_vector
        denominator = qml.math.sqrt(2 + 2 * psi[0])
        summed_array = np.zeros(qml.math.shape(psi), dtype=qml.math.get_dtype_name(psi))
        summed_array[0] = 1.0
        psi = psi + summed_array
        psi /= denominator
        u = 2 * qml.math.outer(psi, qml.math.conj(psi)) - qml.math.eye(len(psi))
        return [QubitUnitary(u, wires=wires)]
