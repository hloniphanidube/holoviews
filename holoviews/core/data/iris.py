from __future__ import absolute_import

import datetime
from itertools import product

import iris
from iris.util import guess_coord_axis

import numpy as np

from .interface import Interface
from .grid import GridInterface
from ..ndmapping import (NdMapping, item_check, sorted_context)
from ..spaces import HoloMap, DynamicMap
from .. import util

from holoviews.core.dimension import Dimension


def get_date_format(coord):
    def date_formatter(val, pos=None):
        date = coord.units.num2date(val)
        date_format = Dimension.type_formatters.get(datetime.datetime, None)
        if date_format:
            return date.strftime(date_format)
        else:
            return date

    return date_formatter


def coord_to_dimension(coord):
    """
    Converts an iris coordinate to a HoloViews dimension.
    """
    kwargs = {}
    if coord.units.is_time_reference():
        kwargs['value_format'] = get_date_format(coord)
    else:
        kwargs['unit'] = str(coord.units)
    return Dimension(coord.name(), **kwargs)


def sort_coords(coord):
    """
    Sorts a list of DimCoords trying to ensure that
    dates and pressure levels appear first and the
    longitude and latitude appear last in the correct
    order.
    """
    order = {'T': -2, 'Z': -1, 'X': 1, 'Y': 2}
    axis = guess_coord_axis(coord)
    return (order.get(axis, 0), coord and coord.name())



class CubeInterface(GridInterface):
    """
    The CubeInterface provides allows HoloViews to interact with iris
    Cube data. When passing an iris Cube to a HoloViews Element the
    init method will infer the dimensions of the Cube from its
    coordinates. Currently the interface only provides the basic
    methods required for HoloViews to work with an object.
    """

    types = (iris.cube.Cube,)

    datatype = 'cube'

    @classmethod
    def init(cls, eltype, data, kdims, vdims):
        if kdims:
            kdim_names = [kd.name if isinstance(kd, Dimension) else kd for kd in kdims]
        else:
            kdim_names = [kd.name for kd in eltype.kdims]

        if not isinstance(data, iris.cube.Cube):
            ndims = len(kdim_names)
            kdims = [kd if isinstance(kd, Dimension) else Dimension(kd)
                     for kd in kdims]
            vdim = vdims[0].name if isinstance(vdims[0], Dimension) else vdims[0]
            if isinstance(data, tuple):
                value_array = data[-1]
                data = {d: vals for d, vals in zip(kdim_names + [vdim], data)}
            elif isinstance(data, dict):
                value_array = data[vdim]
            coords = [(iris.coords.DimCoord(data[kd.name], long_name=kd.name,
                                            units=kd.unit), ndims-n-1)
                      for n, kd in enumerate(kdims)]
            try:
                data = iris.cube.Cube(value_array, long_name=vdim,
                                      dim_coords_and_dims=coords)
            except:
                pass
            if not isinstance(data, iris.cube.Cube):
                raise TypeError('Data must be be an iris Cube type.')

        if kdims:
            coords = []
            for kd in kdims:
                coord = data.coords(kd.name if isinstance(kd, Dimension) else kd)
                if len(coord) == 0:
                    raise ValueError('Key dimension %s not found in '
                                     'Iris cube.' % kd)
                coords.append(coord[0])
        else:
            coords = data.dim_coords
            coords = sorted(coords, key=sort_coords)
        kdims = [coord_to_dimension(crd) for crd in coords]
        if vdims is None:
            vdims = [Dimension(data.name(), unit=str(data.units))]

        return data, {'kdims':kdims, 'vdims':vdims}, {'group':data.name()}


    @classmethod
    def validate(cls, dataset):
        pass


    @classmethod
    def get_coords(cls, dataset, dim, ordered=False):
        data = dataset.data.coords(dim)[0].points
        if ordered and np.all(data[1:] < data[:-1]):
            data = data[::-1]
        return data


    @classmethod
    def values(cls, dataset, dim, expanded=True, flat=True):
        """
        Returns an array of the values along the supplied dimension.
        """
        dim = dataset.get_dimension(dim)
        if dim in dataset.vdims:
            coord_names = [c.name() for c in dataset.data.dim_coords]
            data = dataset.data.copy().data.T
            data = cls.canonicalize(dataset, data, coord_names)
            return data.T.flatten() if flat else data
        elif expanded:
            data = cls.expanded_coords(dataset, dim)
            return data.flatten() if flat else data
        else:
            return cls.get_coords(dataset, dim.name, True)


    @classmethod
    def reindex(cls, dataset, kdims=None, vdims=None):
        """
        Since cubes are never indexed directly the data itself
        does not need to be reindexed, the Element can simply
        reorder its key dimensions.
        """
        dropped = {d.name: cls.values(dataset, d, False)[0]
                   for d in dataset.kdims if d not in kdims
                   and len(cls.values(dataset, d, False)) == 1}
        if dropped:
            constraints = iris.Constraint(**dropped)
            return dataset.data.extract(constraints)
        else:
            return dataset.data


    @classmethod
    def groupby(cls, dataset, dims, container_type=HoloMap, group_type=None, **kwargs):
        """
        Groups the data by one or more dimensions returning a container
        indexed by the grouped dimensions containing slices of the
        cube wrapped in the group_type. This makes it very easy to
        break up a high-dimensional dataset into smaller viewable chunks.
        """
        if not isinstance(dims, list): dims = [dims]
        dynamic = kwargs.pop('dynamic', False)
        dims = [dataset.get_dimension(d) for d in dims]
        constraints = [d.name for d in dims]
        slice_dims = [d for d in dataset.kdims if d not in dims]

        unique_coords = product(*[cls.values(dataset, d, expanded=False)
                                  for d in dims])
        data = []
        for key in unique_coords:
            constraint = iris.Constraint(**dict(zip(constraints, key)))
            cube = dataset.clone(dataset.data.extract(constraint),
                                  new_type=group_type,
                                  **dict(kwargs, kdims=slice_dims))
            data.append((key, cube))
        if issubclass(container_type, NdMapping):
            with item_check(False), sorted_context(False):
                return container_type(data, kdims=dims)
        else:
            return container_type(data)


    @classmethod
    def range(cls, dataset, dimension):
        """
        Computes the range along a particular dimension.
        """
        dim = dataset.get_dimension(dimension)
        values = dataset.dimension_values(dim, False)
        return (np.nanmin(values), np.nanmax(values))


    @classmethod
    def redim(cls, dataset, dimensions):
        """
        Rename coords on the Cube.
        """
        new_dataset = dataset.data.copy()
        for name, new_dim in dimensions.items():
            if name == new_dataset.name():
                new_dataset.rename(new_dim.name)
            for coord in new_dataset.dim_coords:
                if name == coord.name():
                    coord.rename(new_dim.name)
        return new_dataset


    @classmethod
    def length(cls, dataset):
        """
        Returns the total number of samples in the dataset.
        """
        return np.product([len(d.points) for d in dataset.data.coords()])


    @classmethod
    def sort(cls, columns, by=[]):
        """
        Cubes are assumed to be sorted by default.
        """
        return columns


    @classmethod
    def aggregate(cls, columns, kdims, function, **kwargs):
        """
        Aggregation currently not implemented.
        """
        raise NotImplementedError


    @classmethod
    def sample(cls, dataset, samples=[]):
        """
        Sampling currently not implemented.
        """
        raise NotImplementedError


    @classmethod
    def add_dimension(cls, columns, dimension, dim_pos, values, vdim):
        """
        Adding value dimensions not currently supported by iris interface.
        Adding key dimensions not possible on dense interfaces.
        """
        if not vdim:
            raise Exception("Cannot add key dimension to a dense representation.")
        raise NotImplementedError


    @classmethod
    def select_to_constraint(cls, selection):
        """
        Transform a selection dictionary to an iris Constraint.
        """
        constraint_kwargs = {}
        for dim, constraint in selection.items():
            if isinstance(constraint, slice):
                constraint = (constraint.start, constraint.stop)
            if isinstance(constraint, tuple):
                constraint = iris.util.between(*constraint, rh_inclusive=False)
            constraint_kwargs[dim] = constraint
        return iris.Constraint(**constraint_kwargs)


    @classmethod
    def select(cls, dataset, selection_mask=None, **selection):
        """
        Apply a selection to the data.
        """
        constraint = cls.select_to_constraint(selection)
        pre_dim_coords = [c.name() for c in dataset.data.dim_coords]
        indexed = cls.indexed(dataset, selection)
        extracted = dataset.data.extract(constraint)
        if indexed and not extracted.dim_coords:
            return extracted.data.item()
        post_dim_coords = [c.name() for c in extracted.dim_coords]
        dropped = [c for c in pre_dim_coords if c not in post_dim_coords]
        for d in dropped:
            extracted = iris.util.new_axis(extracted, d)
        return extracted


Interface.register(CubeInterface)
