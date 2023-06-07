"""Module to describe wave function transformations."""
from __future__ import annotations

import itertools
from abc import abstractmethod
from functools import partial, reduce
from typing import TYPE_CHECKING, Iterator, TypeVar

import dask.array.core
import numpy as np

from abtem.core.axes import AxisMetadata, ParameterAxis
from abtem.core.backend import get_array_module
from abtem.core.chunks import Chunks, validate_chunks
from abtem.core.ensemble import Ensemble
from abtem.core.fft import ifft2
from abtem.core.utils import (
    CopyMixin,
    EqualityMixin,
    expand_dims_to_broadcast,
)
from abtem.distributions import (
    EnsembleFromDistributions,
    _validate_distribution,
    BaseDistribution,
)

if TYPE_CHECKING:
    from abtem.waves import Waves
    from abtem.array import ArrayObject


T = TypeVar("T", bound="ArrayObject")


class ArrayObjectTransform(Ensemble, CopyMixin):
    @property
    def _num_outputs(self) -> int:
        return 1

    @property
    def metadata(self) -> dict:
        """Metadata added to the waves when applying the transform."""
        return {}

    @property
    def ensemble_shape(self) -> tuple[int, ...]:
        """The shape of the ensemble axes added to the waves when applying the transform."""
        return ()

    @property
    def ensemble_axes_metadata(self) -> list[AxisMetadata]:
        """Axes metadata describing the ensemble axes added to the waves when applying the transform."""
        return []

    def _out_meta(self, array_object: T, index: int = 0) -> np.ndarray:
        """
        The meta describing the measurement array created when detecting the given waves.

        Parameters
        ----------
        array_object : ArrayObject
            The array object to derive the measurement meta from.

        Returns
        -------
        meta : array-like
            Empty array.
        """
        xp = get_array_module(array_object.device)
        return xp.array((), dtype=self._out_dtype(array_object))

    def _out_metadata(self, array_object: T, index: int = 0) -> dict:
        """
        Metadata added to the measurements created when detecting the given waves.

        Parameters
        ----------
        array_object : ArrayObject
            The array object to derive the metadata from.

        Returns
        -------
        metadata : dict
        """
        return array_object.metadata

    def _out_dtype(
        self, array_object: ArrayObject | T, index: int = 0
    ) -> type[np.dtype]:
        """Datatype of the output array."""
        return array_object.dtype

    def _out_type(self, array_object: T, index: int = 0) -> type[T]:
        """
        The subtype of the created array object after applying the transform.

        Parameters
        ----------
        array_object : ArrayObject
            The waves to derive the measurement shape from.

        Returns
        -------
        measurement_type : type of :class:`BaseMeasurements`
        """
        return array_object.__class__

    def _out_ensemble_shape(self, array_object: T) -> tuple[int, ...]:
        """
        Shape of the measurements created when detecting the given waves.

        Parameters
        ----------
        array_object : ArrayObject
            The array object to derive the shape of the output array object from.

        Returns
        -------
        measurement_shape : tuple of int
        """

        return self.ensemble_shape + array_object.ensemble_shape

    def _out_base_shape(self, array_object: T, index: int = 0) -> tuple[int, ...]:
        """
        Shape of the array object created by the transformation.

        Parameters
        ----------
        array_object : ArrayObject
            The waves to derive the measurement shape from.

        Returns
        -------
        measurement_shape : tuple of int
        """
        return array_object.base_shape

    def _out_shape(self, array_object: T, index: int = 0) -> tuple[int, ...]:
        return self._out_ensemble_shape(array_object) + self._out_base_shape(
            array_object, index
        )

    def _out_base_axes_metadata(
        self, array_object: T, index: int = 0
    ) -> list[AxisMetadata]:
        """
        Axes metadata of the created measurements when detecting the given waves.

        Parameters
        ----------
        array_object: ArrayObject
            The waves to derive the measurement shape from.

        Returns
        -------
        axes_metadata : list of :class:`AxisMetadata`
        """
        return array_object.base_axes_metadata

    def _out_ensemble_axes_metadata(
        self, array_object: ArrayObject | T
    ) -> list[AxisMetadata] | tuple[list[AxisMetadata], ...]:
        return self.ensemble_axes_metadata + array_object.ensemble_axes_metadata

    def __add__(self, other: ArrayObjectTransform) -> CompositeArrayObjectTransform:
        transforms = []

        for transform in (self, other):

            if hasattr(transform, "transforms"):
                transforms += transform.transforms
            else:
                transforms += [transform]

        return CompositeArrayObjectTransform(transforms)

    def _out_axes_metadata(self, array_object: T):
        return self._out_ensemble_axes_metadata(
            array_object
        ) + self._out_base_axes_metadata(array_object)

    @staticmethod
    def _extract(array, index):
        array = array.item()[index]
        return array

    def _pack_multiple_outputs(self, array_object, new_arrays):
        ensemble_axes_metadata = self._out_ensemble_axes_metadata(array_object)
        ensemble_shape = self._out_ensemble_shape(array_object)

        is_lazy = isinstance(new_arrays, dask.array.core.Array)
        if is_lazy:
            assert ensemble_shape == new_arrays.shape

        outputs = ()
        for output_index in range(self._num_outputs):
            base_shape = self._out_base_shape(array_object, output_index)
            meta = self._out_meta(array_object, output_index)
            cls = self._out_type(array_object, output_index)
            metadata = self._out_metadata(array_object, output_index)
            base_axes_metadata = self._out_base_axes_metadata(
                array_object, output_index
            )

            if is_lazy:
                new_axis = tuple(
                    range(len(ensemble_shape), len(ensemble_shape) + len(base_shape))
                )

                ensemble_chunks = new_arrays.chunks

                base_chunks = tuple((n,) for n in base_shape)

                new_array = new_arrays.map_blocks(
                    self._extract,
                    output_index,
                    chunks=ensemble_chunks + base_chunks,
                    new_axis=new_axis,
                    meta=meta,
                )
            else:
                new_array = new_arrays[output_index]

            axes_metadata = ensemble_axes_metadata + base_axes_metadata

            output = cls.from_array_and_metadata(
                new_array, axes_metadata=axes_metadata, metadata=metadata
            )

            outputs += (output,)

        return outputs

    def _pack_single_output(
        self,
        array_object: T,
        new_array: np.ndarray,
    ) -> T:

        ensemble_axes_metadata = self._out_ensemble_axes_metadata(array_object)

        base_axes_metadata = self._out_base_axes_metadata(array_object)

        axes_metadata = ensemble_axes_metadata + base_axes_metadata

        metadata = self._out_metadata(array_object)

        cls = self._out_type(array_object)

        array_object = cls.from_array_and_metadata(
            new_array, axes_metadata=axes_metadata, metadata=metadata
        )

        return array_object

    def _calculate_new_array(
        self, array_object: T
    ) -> np.ndarray | tuple[np.ndarray, ...]:
        raise NotImplementedError

    def apply(self, array_object: T) -> T | tuple[T, ...]:
        """
        Apply the transform to the given waves.

        Parameters
        ----------
        array_object : ArrayObject
            The array object to transform.

        Returns
        -------
        transformed_array_object : ArrayObject
        """
        new_array = self._calculate_new_array(array_object)
        if self._num_outputs > 1:
            return self._pack_multiple_outputs(array_object, new_array)
        else:
            return self._pack_single_output(array_object, new_array)


class EnsembleTransform(EnsembleFromDistributions, ArrayObjectTransform):
    def __init__(self, distributions: tuple[str, ...] = ()):
        super().__init__(distributions=distributions)

    @staticmethod
    def _validate_distribution(distribution):
        return _validate_distribution(distribution)

    @property
    def ensemble_axes_metadata(self):
        return []

    def _axes_metadata_from_distributions(self, **kwargs):
        ensemble_axes_metadata = []
        for distribution_name in self._distributions:
            distribution = getattr(self, distribution_name)
            if isinstance(distribution, BaseDistribution):
                axis_kwargs = kwargs[distribution_name]
                ensemble_axes_metadata += [
                    ParameterAxis(
                        values=distribution,
                        _ensemble_mean=distribution.ensemble_mean,
                        **axis_kwargs,
                    )
                ]

        return ensemble_axes_metadata

        # kwargs = array_object._copy_kwargs(exclude=("array",) + ())
        # kwargs["ensemble_axes_metadata"] = (
        #    self.ensemble_axes_metadata + kwargs["ensemble_axes_metadata"]
        # )
        # kwargs["metadata"].update(self.metadata)
        # return array_object.__class__(new_array, **kwargs)


class WavesTransform(EnsembleTransform):
    def apply(self, waves: Waves) -> Waves:
        waves = super().apply(waves)
        return waves


class CompositeArrayObjectTransform(ArrayObjectTransform):
    """
    Combines multiple array object transformations into a single transformation.

    Parameters
    ----------
    transforms : ArrayObject
        The array object to transform.
    """

    def __init__(
        self,
        transforms: list[ArrayObjectTransform] = None,
        base_shape: tuple[int, ...] = None,
        ensemble_shape: tuple[int, ...] = None,
        base_axes_metadata: list[AxisMetadata] = None,
        ensemble_axes_metadata: list[AxisMetadata] = None,
        meta: np.ndarray = None,
        metadata: dict = None,
    ):
        if transforms is None:
            transforms = []

        self._transforms = transforms
        self._base_shape = base_shape
        self._ensemble_shape = ensemble_shape
        self._base_axes_metadata = base_axes_metadata
        self._ensemble_axes_metadata = ensemble_axes_metadata
        self._meta = meta
        self._metadata = metadata
        super().__init__()

    @property
    def _num_outputs(self) -> int:
        return self._transforms[-1]._num_outputs

    def insert(
        self, transform: ArrayObjectTransform, index: int
    ) -> CompositeArrayObjectTransform:
        """
        Inserts an array object transform to the sequence of transforms before the specified index.

        Parameters
        ----------
        transform : ArrayObjectTransform
            Array object transform to insert.
        index : int
            The array object transform is inserted before this index.

        Returns
        -------
        composite_array_transform : CompositeArrayObjectTransform
        """
        self._transforms.insert(index, transform)
        return self

    def __len__(self) -> int:
        return len(self.transforms)

    def __iter__(self) -> Iterator[ArrayObjectTransform]:
        return iter(self.transforms)

    def _out_metadata(self, array_object, index=0):
        if self._metadata is not None:
            return self._metadata

        metadata = [
            transform._out_metadata(array_object, index)
            for transform in self.transforms
        ]
        return reduce(lambda a, b: {**a, **b}, metadata)

    @property
    def ensemble_axes_metadata(self) -> list[AxisMetadata]:
        ensemble_axes_metadata = [
            wave_transform.ensemble_axes_metadata
            for i, wave_transform in enumerate(self.transforms)
        ]

        ensemble_axes_metadata = list(itertools.chain(*ensemble_axes_metadata))
        return ensemble_axes_metadata

    def _out_ensemble_axes_metadata(self, array_object) -> list[AxisMetadata]:
        if self._ensemble_axes_metadata is not None:
            return self._ensemble_axes_metadata
        return self.ensemble_axes_metadata + array_object.ensemble_axes_metadata

    def _out_base_axes_metadata(self, array_object, index=0) -> list[AxisMetadata]:
        if self._base_axes_metadata is not None:
            return self._base_axes_metadata
        return self._transforms[-1]._out_base_axes_metadata(array_object, index)

    @property
    def ensemble_shape(self) -> tuple[int, ...]:
        ensemble_shape = [transform.ensemble_shape for transform in self.transforms]
        return tuple(itertools.chain(*ensemble_shape))

    def _out_ensemble_shape(self, array_object) -> tuple[int, ...]:
        ensemble_shape = self.ensemble_shape + array_object.ensemble_shape
        return ensemble_shape

    def _out_base_shape(self, array_object, index=0):
        if self._base_shape is not None:
            return self._base_shape

        return self._transforms[-1]._out_base_shape(array_object, index)

    def _out_meta(self, array_object, index=0):
        return self._transforms[-1]._out_meta(array_object, index)

    def _out_dtype(self, array_object, index=0):
        return self._out_meta(array_object).dtype

    def _out_type(self, array_object, index=0):
        return self._transforms[-1]._out_type(array_object, index)

    @property
    def transforms(self) -> list[ArrayObjectTransform]:
        """The list of transforms in the composite."""
        return self._transforms

    @property
    def _default_ensemble_chunks(self) -> Chunks:
        default_ensemble_chunks = [
            transform._default_ensemble_chunks for transform in self.transforms
        ]
        return tuple(itertools.chain(*default_ensemble_chunks))

    def _calculate_new_array(
        self, array_object: T
    ) -> np.ndarray | tuple[np.ndarray, ...]:

        # for transform in reversed(self.transforms):
        #    array_object = transform.apply(array_object)

        for transform in self.transforms:
            array_object = transform.apply(array_object)

        if self._num_outputs > 1:
            return tuple(array_object[i].array for i in range(self._num_outputs))
        else:
            return array_object.array

    def _partition_args(self, chunks=None, lazy: bool = True):
        if chunks is None:
            chunks = self._default_ensemble_chunks

        chunks = validate_chunks(self.ensemble_shape, chunks, limit="auto")

        chunks = self._validate_chunks(chunks)

        blocks = ()
        start = 0
        for transform in self.transforms:
            stop = start + len(transform.ensemble_shape)
            blocks += transform._partition_args(chunks[start:stop], lazy=lazy)
            start = stop

        return blocks

    @staticmethod
    def _partial(*args, partials):
        wave_transfer_functions = []
        for p in partials:
            wave_transfer_functions += [p[0](*[args[i] for i in p[1]])]

        return CompositeArrayObjectTransform(wave_transfer_functions)

    def _from_partitioned_args(self):
        partials = ()
        i = 0
        for wave_transform in self.transforms:
            arg_indices = tuple(range(i, i + len(wave_transform.ensemble_shape)))
            partials += ((wave_transform._from_partitioned_args(), arg_indices),)
            i += len(arg_indices)

        return partial(self._partial, partials=partials)


class ReciprocalSpaceMultiplication(WavesTransform):
    """


    Parameters
    ----------
    in_place: bool, optional
        If True, the array representing the waves may be modified in-place.
    distributions : tuple of str, optional
        Names of properties that may be described by a distribution.
    """

    def __init__(
        self,
        in_place: bool = False,
        distributions: tuple[str, ...] = (),
    ):

        self._in_place = in_place
        super().__init__(distributions=distributions)

    @property
    def in_place(self) -> bool:
        return self._in_place

    @abstractmethod
    def _evaluate_kernel(self, array_object):
        pass

    def _calculate_new_array(self, waves: Waves) -> np.ndarray:
        real_space_in = not waves.reciprocal_space

        waves = waves.ensure_reciprocal_space(overwrite_x=self.in_place)
        kernel = self._evaluate_kernel(waves)

        kernel, new_array = expand_dims_to_broadcast(
            kernel, waves.array, match_dims=[(-2, -1), (-2, -1)]
        )

        xp = get_array_module(waves.array)

        kernel = xp.array(kernel)

        if self.in_place:
            new_array *= kernel
        else:
            new_array = new_array * kernel

        if real_space_in:
            new_array = ifft2(new_array, overwrite_x=self.in_place)

        return new_array
