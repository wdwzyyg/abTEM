"""Module to describe wave function transformations."""
from __future__ import annotations
import itertools
from abc import abstractmethod
from functools import partial, reduce
from typing import TYPE_CHECKING
import dask
import numpy as np
import dask.array as da

from abtem.core.backend import get_array_module
from abtem.core.device import HasDeviceMixin
from abtem.core.energy import (
    HasAcceleratorMixin,
    Accelerator,
    reciprocal_space_sampling_to_angular_sampling,
)
from abtem.core.ensemble import Ensemble
from abtem.core.grid import HasGridMixin, polar_spatial_frequencies, Grid
from abtem.core.utils import (
    CopyMixin,
    EqualityMixin,
)
from abtem.distributions import _EnsembleFromDistributionsMixin

if TYPE_CHECKING:
    from abtem.waves import Waves, BaseWaves


class WaveTransform(Ensemble, EqualityMixin, CopyMixin):
    @property
    def metadata(self):
        """Metadata added to the waves when applying the transform."""
        return {}

    @property
    @abstractmethod
    def ensemble_shape(self):
        """The shape of the ensemble axes added to the waves when applying the transform."""
        pass

    @property
    @abstractmethod
    def ensemble_axes_metadata(self):
        """Axes metadata describing the ensemble axes added to the waves when applying the transform."""
        pass

    def __add__(self, other: WaveTransform) -> CompositeWaveTransform:
        wave_transforms = []

        for wave_transform in (self, other):

            if hasattr(wave_transform, "wave_transforms"):
                wave_transforms += wave_transform.wave_transforms
            else:
                wave_transforms += [wave_transform]

        return CompositeWaveTransform(wave_transforms)

    @abstractmethod
    def apply(
        self, waves: Waves, **kwargs
    ) -> Waves:
        """
        Apply the transform to the given waves.

        Parameters
        ----------
        waves : Waves
            The waves to transform.
        kwargs :
            Optional keyword arguments used by the transform.

        Returns
        -------
        transformed_waves : Waves
        """
        pass


class CompositeWaveTransform(WaveTransform):
    def __init__(self, wave_transforms: list[WaveTransform] = None):

        if wave_transforms is None:
            wave_transforms = []

        self._wave_transforms = wave_transforms
        super().__init__()

    def insert_transform(self, transform, index):
        self._wave_transforms.insert(transform, index)

    def __len__(self):
        return len(self.wave_transforms)

    def __iter__(self):
        return iter(self.wave_transforms)

    @property
    def metadata(self):
        metadata = [transform.metadata for transform in self.wave_transforms]
        return reduce(lambda a, b: {**a, **b}, metadata)

    @property
    def wave_transforms(self):
        return self._wave_transforms

    @property
    def ensemble_axes_metadata(self):
        ensemble_axes_metadata = [
            wave_transform.ensemble_axes_metadata
            for wave_transform in self.wave_transforms
        ]
        return list(itertools.chain(*ensemble_axes_metadata))

    @property
    def _default_ensemble_chunks(self):
        default_ensemble_chunks = [
            wave_transform._default_ensemble_chunks
            for wave_transform in self.wave_transforms
        ]
        return tuple(itertools.chain(*default_ensemble_chunks))

    @property
    def ensemble_shape(self):
        ensemble_shape = [
            wave_transform.ensemble_shape for wave_transform in self.wave_transforms
        ]
        return tuple(itertools.chain(*ensemble_shape))

    def apply(self, waves: BaseWaves, in_place: bool = False):
        waves.grid.check_is_defined()

        for wave_transform in reversed(self.wave_transforms):
            waves = wave_transform.apply(waves, in_place=in_place)

        return waves

    def _partition_args(self, chunks=None, lazy: bool = True):
        if chunks is None:
            chunks = self._default_ensemble_chunks

        chunks = self._validate_chunks(chunks)

        blocks = ()
        start = 0
        for wave_transform in self.wave_transforms:
            stop = start + len(wave_transform.ensemble_shape)
            blocks += wave_transform._partition_args(chunks[start:stop], lazy=lazy)
            start = stop

        return blocks

    @staticmethod
    def ctf(*args, partials):
        wave_transfer_functions = []
        for p in partials:
            wave_transfer_functions += [p[0](*[args[i] for i in p[1]])]

        return CompositeWaveTransform(wave_transfer_functions)

    def _from_partitioned_args(self):
        partials = ()
        i = 0
        for wave_transform in self.wave_transforms:
            arg_indices = tuple(range(i, i + len(wave_transform.ensemble_shape)))
            partials += ((wave_transform._from_partitioned_args(), arg_indices),)
            i += len(arg_indices)

        return partial(self.ctf, partials=partials)


class EnsembleTransform(_EnsembleFromDistributionsMixin, WaveTransform):
    def __init__(self, distributions):
        super().__init__(distributions=distributions)

    def _pack_array(self, x, array):
        kwargs = x._copy_kwargs(exclude=("array",))
        kwargs["ensemble_axes_metadata"] = (
                self.ensemble_axes_metadata + kwargs["ensemble_axes_metadata"]
        )
        kwargs["metadata"].update(self.metadata)
        return x.__class__(array, **kwargs)

    @abstractmethod
    def apply(
        self,
        x,
        **kwargs
    ):
        pass

class FourierSpaceConvolution(
    WaveTransform, HasAcceleratorMixin, HasGridMixin, HasDeviceMixin
):
    def __init__(
        self,
        energy: float,
        extent: float | tuple[float, float] = None,
        gpts: int | tuple[int, int] = None,
        sampling: float | tuple[float, float] = None,
        device: str = "cpu",
        **kwargs
    ):
        self._accelerator = Accelerator(energy=energy, **kwargs)
        self._grid = Grid(extent=extent, gpts=gpts, sampling=sampling)
        self._device = device

    @abstractmethod
    def _evaluate_with_alpha_and_phi(self, alpha, phi):
        pass

    @property
    def angular_sampling(self):
        return reciprocal_space_sampling_to_angular_sampling(
            self.reciprocal_space_sampling, self.energy
        )

    def _angular_grid(self):
        xp = get_array_module(self._device)
        alpha, phi = polar_spatial_frequencies(self.gpts, self.sampling, xp=xp)
        alpha *= self.wavelength
        return alpha, phi

    def _evaluate(self):
        alpha, phi = self._angular_grid()
        return self._evaluate_with_alpha_and_phi(alpha, phi)

    def evaluate(self, waves: Waves = None, lazy: bool = False) -> np.ndarray:
        """
        Evaluate the array to be multiplied with the waves in reciprocal space.

        Parameters
        ----------
        waves : Waves, optional
            If given, the array will be evaluated to match the provided waves.
        lazy : bool, optional
            If True, the array is lazily evaluated, a Dask array is returned.

        Returns
        -------
        kernel : np.ndarray or dask.array.Array
        """
        if waves is not None:
            self.accelerator.match(waves)
            self.grid.match(waves)

        if lazy:
            array = dask.delayed(self._evaluate)()
            array = da.from_delayed(
                array, dtype=np.complex64, shape=self.ensemble_shape + self.gpts
            )
            return array
        else:
            return self._evaluate()

    def apply(self, waves: Waves, in_place: bool = False, **kwargs) -> Waves:
        """
        Transform the waves using Fourier space convolution.

        Parameters
        ----------
        waves : Waves
            The waves to transform.
        in_place : bool, optional
            If True, the array representing the waves may be modified in-place.
        Returns
        -------
        transformed_waves : Waves
        """
        axes_metadata = self.ensemble_axes_metadata
        array = self.evaluate(waves)
        return waves.convolve(array, axes_metadata, in_place=in_place)
