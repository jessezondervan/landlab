#!/usr/bin/env python
"""Simulate lineages in response to landscape evolution.

Landlab component that manages species and evolves the species over time.

Component written by Nathan Lyons beginning August 2017.
"""
from collections import defaultdict, OrderedDict
from itertools import product
from string import ascii_uppercase

import numpy as np
from pandas import DataFrame

from landlab import Component
from landlab.core.messages import warning_message
from .zone import ZoneManager


class SpeciesEvolver(Component):
    """Simulate the macroevolutionary processes of species.

    This component evolves the species introduced to a model grid. The
    evolutionary rules are coded in `Species` objects. The geographic range
    of species is controlled by `Zone` objects. Zones are located, created, and
    updated  by `ZoneManager` objects. Species, Zones, and ZoneManagers are
    designed to be subclassed to expand functionality.

    The standard workflow provides basic functionality. The steps of the
    standard workflow:

    1.  Create a 'zone_mask' field at model grid nodes. Set the nodes that meet
        zone conditions to `True`.
    2.  Instantiate the component. The initial zones demarcated by the field,
        `zone_mask` are identified and created by a zone manager when the
        component is initialized. A zone is created for each cluster of
        adjacent nodes set to `True` in this mask.
    3.  Get the initial zones using the :func:`get_zones_at_time` method.
    4.  Instantiate the initial species. The initial zone(s) of the species are
        set as species are initialized.
    5.  Introduce the intial species. Use the :func:`introduce_species` method
        to do so. Species can be seeded to the initial zones created in the
        prior step.
    6.  Increment the model. The primary method of this class is
        :func:`run_one_step`. The temporal connectivity of zones is identified
        by this method and stored in `zone_paths`. The Species method
        :func:`evolve` is called through :func:`run_one_step`.

    Flexibility is provided by the standard workflow. The spatial distribution
    of zones can be updated at each time step. The count and spatial
    distribution of species can be set as desired at model onset and later time
    steps. Species can be the base class or custom subclasses. Limitations of
    the standard workflow include zones cannot overlap, and a singular zone and
    zone manager type. These limitations can be overcome by adapting the
    workflow.

    This component tracks model time to construct lineage phylogeny. Time is
    unitless within the component, and for example, can be thought of as in
    years or time steps. Time is advanced using the *dt* parameter in
    `run_one_step`.

    Time and other variables can be viewed in the class attribute, *record*.
    Zones and species can send variables in a 'record_add_on' at each time
    step. Metadata of the species and zones of a component instance can be
    viewed with the attributes, `species` and `zones`, respectively.

    Species are assigned identifiers in the order that they are introduced and
    created by parent species. Clades are lettered from A to Z then AA to AZ
    and so forth. Clade members are numbered in the order of appearance. For
    example, the first species introduced is A.0 and if that species speciates,
    the first child species is A.1.

    This component is inspired by SEAMLESS (Spatially Explicit Area Model of
    Landscape Evolution by SimulationS). See Albert et al., 2017, Systematic
    Biology 66.
    """
    _name = 'SpeciesEvolver'

    def __init__(self, grid, initial_time=0, zone_managers=None):
        """Instantiate SpeciesEvolver.

        Parameters
        ----------
        grid : ModelGrid
            A Landlab ModelGrid.
        initial_time : int or float, optional
            The initial time. The default is 0.
        zone_managers : ZoneManager list, optional
            A list of SpeciesEvolver ZoneManagers. If 'None` is specified, a
            ZoneManager instance will be created that requires *grid* to have a
            'zone_mask' field at the nodes of *grid*.

        Examples
        --------
        >>> from landlab import RasterModelGrid
        >>> from landlab.components import SpeciesEvolver
        >>> from landlab.components.species_evolution import (Species,
        ...                                                   ZoneManager)

        Create a model grid with mountain scale resolution.

        >>> mg = RasterModelGrid((3, 7), 1000)
        >>> z = mg.add_ones('node', 'topographic__elevation')
        >>> z.reshape(mg.shape)
        array([[ 1.,  1.,  1.,  1.,  1.,  1.,  1.],
               [ 1.,  1.,  1.,  1.,  1.,  1.,  1.],
               [ 1.,  1.,  1.,  1.,  1.,  1.,  1.]])

        By default, the field, 'zone_mask' is expected. This field is a boolean
        array where `True` values represents nodes that satisfying zone
        conditions. A zone object is not created here. Only the extent of this
        zone type is defined here.

        >>> mg.at_node['zone_mask'] = z < 100

        Instantiate the component with parameters, the grid and a list of
        zone managers. The initial zones are created at instantiation. In this
        example, one zone is created because all nodes of the zone mask are
        adjacent to each other.

        >>> se = SpeciesEvolver(mg)
        >>> zones = se.zones_at_time(0)
        >>> len(zones) == 1
        True

        All nodes of the grid are included because the elevation of each node
        is below 100 units.

        . . . . . . .       key:    . node in the initial zone
        . . . . . . .
        . . . . . . .

        Seed the zone with a species.

        >>> new_species = Species(zones[0])
        >>> se.introduce_species(new_species)
        >>> len(se.species_at_time(0)) == 1
        True

        Drive a change in the zone mask to demonstrate component functionality.
        Here we begin a new time step where topography is uplifted by 200 units
        forming a ridge that trends north-south in the center of the grid.

        >>> z[[3, 10, 17]] = 200

        The elevation after uplift is represented here.

        - - - ^ - - -       elevation:  - 1
        - - - ^ - - -                   ^ 200
        - - - ^ - - -

        The zone mask field is updated to reflect the elevation change.

        >>> mg.at_node['zone_mask'] = z < 100

        The updated zone mask is below.

        . . . x . . .       key:    . node in zone mask
        . . . x . . .               x node outside of zone mask
        . . . x . . .

        Run a step.

        >>> dt = 1
        >>> se.run_one_step(dt)
        >>> zones = se.zones_at_time(1)
        >>> len(zones) == 2
        True

        A new zone was created because the zone mask was not continuous.

        . . . ^ * * *       key:    . a zone
        . . . ^ * * *               * another zone
        . . . ^ * * *               ^ mountain range

        The split of the initial zone triggered speciation.

        >>> len(se.species_at_time(1)) == 2
        True
        """
        Component.__init__(self, grid)

        # Create data structures.

        self._record = OrderedDict([('time', [initial_time])])

        self._species = OrderedDict([('clade', []),
                                     ('species_number', []),
                                     ('time_appeared', []),
                                     ('latest_time', []),
                                     ('object', [])])

        self._zones = OrderedDict([('time_appeared', []),
                                   ('latest_time', []),
                                   ('object', [])])

        # Instantiate a dictionary to track the clade names (dict keys) and
        # their maximum species number (dict values).

        self._species_ids = {}

        # Set the zone_managers. Create a default zone manager if
        # *zone_managers* was not specified.

        if zone_managers == None:
            # Instantiate a default ZoneManager. An exception will be raised if
            # *grid* does not have a 'zone_mask' field at nodes.
            self._zone_managers = [ZoneManager(grid)]
        else:
            self._zone_managers = zone_managers

        # Set initial zones.

        initial_zones = []

        for zm in self._zone_managers:
            initial_zones.extend(zm._create_zones())

        self._update_zone_data_structure(initial_time, initial_zones)

    # Define attributes

    @property
    def record(self):
        """A DataFrame of SpeciesEvolver variables over time."""
        return DataFrame(self._record)

    @property
    def species(self):
        """A DataFrame of the variables and objects of species."""
        columns = list(self._species.keys())
        columns.remove('object')
        return DataFrame(self._species, columns=columns)

    @property
    def zones(self):
        """A DataFrame of the variables and objects of zones."""
        columns = list(self._zones.keys())
        columns.remove('object')
        return DataFrame(self._zones, columns=columns)

    # Update methods

    def run_one_step(self, dt):
        """Run macroevolution processes for a single timestep.

        Data describing the connectivity of zones over time.

        Parameters
        ----------
        dt : float
            The model time step duration.
        """
        # Insert the new time in the record.

        time = max(self._record['time']) + dt
        self._record['time'].append(time)

        # Get the zones present at *time*.

        new_zones = []
        for zm in self._zone_managers:
            new_zones.extend(zm._create_zones())

        # Reconsile existing and new zones.

        updated_zones = self._get_updated_zones(time, new_zones)
        self._update_zone_data_structure(time, updated_zones)

        # Process species.

        if len(self._species['object']) == 0:
            print(warning_message('No species exist. Introduce species to '
                                  'SpeciesEvolver.'))
        else:
            survivors = self._get_surviving_species(time, dt)
            self._update_species_data_structure(time, survivors)

    def _get_updated_zones(self, time, new_zones):

        # Process only the zones extant at the prior time.

        prior_time = self._get_prior_time()
        prior_zones = self.zones_at_time(prior_time)

        # Create an add on to insert into *record*.

        add_on = defaultdict(float)

        # Get the zones that persist in *time* given zone rules.

        destinations = []

        for zm in self._zone_managers:
            zm_prior_zones = list(filter(lambda z: z._manager == zm,
                                         prior_zones))

            zm_new_zones = list(filter(lambda z: z._manager == zm, new_zones))

            zm_dest = zm._update_paths(time, zm_prior_zones, zm_new_zones,
                                       add_on)

            destinations.extend(zm_dest)

        self._insert_record_add_on(add_on)

        # Return only the unique zone path destinations.

        return list(set(destinations))

    def _get_surviving_species(self, time, dt):

        # Process only the species extant at the prior time.

        prior_time = self._get_prior_time()
        extant_species = self.species_at_time(prior_time)

        # Create an add on to insert into *record*.

        add_on = defaultdict(float)

        # Get the species that persist in *time* given the outcome of the
        # macroevolution processes of the species.

        surviving_species = []

        for es in extant_species:
            child_species = es._evolve(time, dt, add_on)

            if es.extant:
                surviving_species.append(es)

            if len(child_species) > 0:
                surviving_species.extend(child_species)

            # Set id for child species.

            for cs in child_species:
                clade = cs.parent_species.clade
                cs._identifier = self._get_unused_species_id(clade)

        self._insert_record_add_on(add_on)

        return surviving_species

    # Update data structure methods

    def _update_zone_data_structure(self, time, zones_at_time):
        z_updated, z_new = self._object_set_difference(zones_at_time,
                                                       self._zones)

        for z in z_updated:
            # Update the latest time value of the updated zones.
            self._zones['latest_time'][self._zones['object'].index(z)] = time

        if z_new:
            # Add the new zones to the dataframe.
            t = [time] * len(z_new)

            self._zones['time_appeared'].extend(t)
            self._zones['latest_time'].extend(t)
            self._zones['object'].extend(z_new)

    def _update_species_data_structure(self, time, species_at_time):
        s_updated, s_new = self._object_set_difference(species_at_time,
                                                       self._species)

        for s in s_updated:
            # Update the latest time value of the updated species.
            idx = self._species['object'].index(s)
            self._species['latest_time'][idx] = time

        if s_new:
            clade = [s.identifier[0] for s in s_new]
            s_number = [s.identifier[1] for s in s_new]
            t = [time] * len(s_new)

            self._species['clade'].extend(clade)
            self._species['species_number'].extend(s_number)
            self._species['time_appeared'].extend(t)
            self._species['latest_time'].extend(t)
            self._species['object'].extend(s_new)

    def _object_set_difference(self, objects, dataframe):
        objs = set(objects)
        df_objs = set(dataframe['object'])

        # Identify the objects already in the dataframe.
        updated_objects = list(objs.intersection(df_objs))

        # Identify the objects that are new.
        new_objects = list(objs - df_objs)

        return updated_objects, new_objects

    # Species methods

    def introduce_species(self, species):
        """Add a species to SpeciesEvolver.

        The species is introduced at the latest time in the record. It is
        assigned an identifier.

        Parameters
        ----------
        species : Species
            A SpeciesEvolver species to introduce.

        Examples
        --------


        """
        if species in self._species['object']:
            msg = 'The species object, {} was already introduced.'
            raise Exception(msg.format(species))

        if not species._zones:
            # Species with no zones are not introduced.
            msg = ('The species object, {} is not found in any zones. It was '
                   'not introduced.')
            raise Exception(msg.format(species))
            return

        # Set species identifier, `sid`.

        clade_name = self._get_unused_clade_name()
        sid = self._get_unused_species_id(clade_name)
        species._identifier = sid

        # Update the species data structure.

        time = max(self._record['time'])
        self._update_species_data_structure(time, [species])

    def _get_unused_clade_name(self):
        alphabet = list(ascii_uppercase)
        used_name = list(self._species_ids.keys())
        potential_name = np.setdiff1d(alphabet, used_name)

        size = 1
        while len(potential_name) == 0:
            a = product(ascii_uppercase, repeat=size)
            a = [''.join(s) for s in a]
            potential_name = np.setdiff1d(a, used_name)
            size += 1

        clade_name = potential_name[0]

        self._species_ids[clade_name] = None

        return clade_name

    def _get_unused_species_id(self, clade_name):
        if self._species_ids[clade_name] == None:
            self._species_ids[clade_name] = 0
        else:
            self._species_ids[clade_name] += 1
        return (clade_name, self._species_ids[clade_name])

    # Query methods

    def _objects_at_time(self, time, dictionary):
        appeared = np.array(dictionary['time_appeared'])
        latest = np.array(dictionary['latest_time'])

        appeared_before_time = appeared <= time
        present_at_time = latest >= time
        extant_at_time = np.all([appeared_before_time, present_at_time], 0)

        objects = np.array(dictionary['object'])[extant_at_time]

        return list(objects)

    def zones_at_time(self, time):
        """Get the zones that exist at a time.

        Parameters
        ----------
        time : float
            The time in the simulation.

        Returns
        -------
        zones : Zone list
            The SpeciesEvolver zones that exist at *time*.
        """
        return self._objects_at_time(time, self._zones)

    def species_at_time(self, time):
        """Get the species that exist at a time.

        Parameters
        ----------
        time : float
            The time in the simulation.

        Returns
        -------
        species : Species list
            The SpeciesEvolver species that exist at *time*.

        Examples
        --------
        """
        return self._objects_at_time(time, self._species)

    def species_with_identifier(self, identifier_element,
                                return_data_frame=False):
        """Get species using identifiers.

        A singular species is returned when *identifier_element* is a tuple
        with elements that match species in the *species* DataFrame. The
        first element of the tuple is the clade name and the second element is
        the species number.

        The species of a clade are returned when *identifier_element* is
        a string that matches a clade name in the *species* DataFrame.

        The species that share a species number are returned when
        *identifier_element* is an integer that matches species number in the
        *species* DataFrame.

        By default, the species with *identifier_element* will be returned in a
        DataFrame. Alternatively, a list of Species objects can be returned by
        setting *return_objects* to ``True``. A singular species is returned
        when *identifier_element* is a tuple. Otherwise, the species object(s)
        are returned in a list.

        `None` is returned if no species have an identifier that matches
        *identifier_element*.

        Parameters
        ----------
        identifier_element : tuple, str, or int
            The identifier element of the species to return.
        return_objects : boolean, optional
            ``True`` returns species as SpeciesEvolver objects. ``False``, the
            default, returns a DataFrame.

        Returns
        -------
        DataFrame, SpeciesEvolver Species, or SpeciesEvolver Species list
            The species with identifiers that matched *identifier_element*. The
            type of the return object is set by *return_objects*.

        Examples
        --------
        >>> from landlab import RasterModelGrid
        >>> from landlab.components import SpeciesEvolver
        >>> from landlab.components.species_evolution import Species
        >>> import numpy as np
        >>> mg = RasterModelGrid((3, 5))
        >>> zone_id = np.array([np.nan, np.nan, np.nan, np.nan, np.nan,
        ...                     np.nan,      1,      2,      3, np.nan,
        ...                     np.nan, np.nan, np.nan, np.nan, np.nan])
        >>> zone_field = mg.add_field('node', 'zone_id', zone_id)
        >>> se = SpeciesEvolver(mg)
        >>> zones = se.zones_at_time(0, return_objects=True)

        Instantiate and introduce a species to each zone.

        >>> species1 = Species(zones[0])
        >>> species2 = Species(zones[1])
        >>> species3 = Species(zones[2], parent_species=species2)
        >>> se.introduce_species(species1)
        >>> se.introduce_species(species2)
        >>> se.introduce_species(species3)

        Get all the species introduced in a dataframe.

        >>> se.species
          clade species time_appeared latest_time     object
        0     A       0             0           0  <Species>
        1     B       0             0           0  <Species>
        2     B       1             0           0  <Species>

        Get the species, B.0.

        >>> se.species_with_identifier(('B', 0))
          clade species time_appeared latest_time     object
        1     B       0             0           0  <Species>

        Get all of the species in clade, B.

        >>> se.species_with_identifier('B')
          clade species time_appeared latest_time     object
        1     B       0             0           0  <Species>
        2     B       1             0           0  <Species>

        Get all of the species with the same number, 0, despite the clade.

        >>> se.species_with_identifier(0)
          clade species time_appeared latest_time     object
        0     A       0             0           0  <Species>
        1     B       0             0           0  <Species>

        Get the species, B.0 as an object rather than dataframe.

        >>> species_obj = se.species_with_identifier(('B', 0),
                                                     return_objects=True)
        >>> species_obj.identifier
        ('B', 0)
        """
        sdf = DataFrame(self._species)

        element_type = type(identifier_element)

        if element_type == tuple:
            # Get a singular species using a clade name and number.
            clade = identifier_element[0]
            num = identifier_element[1]

            if not np.all([len(identifier_element) == 2,
                           isinstance(clade, str), isinstance(num, int)], 0):
                raise TypeError('*identifier_element* when it is a tuple must '
                                'have a length of 2. The first element must '
                                'be a string, and the second must be an '
                                'integer.')

            s_out = sdf.loc[np.all([sdf.clade == clade,
                                    sdf.species_number == num], 0)]

        elif element_type == str:
            # Get the species of a clade.
            s_out = sdf.loc[sdf.clade == identifier_element]

        elif element_type == int:
            # Get the species that match a number.
            s_out = sdf.loc[sdf.species_number == identifier_element]

        else:
            raise TypeError('*identifier_element* must be a tuple, string, or '
                            'integer.')

        if len(s_out) == 0:
            return None

        if return_data_frame:
            return s_out

        return OrderedDict(s_out.to_dict())

    # Record methods

    def _get_prior_time(self):
        if len(self._record['time']) < 2:
            return np.nan
        else:
            return sorted(self._record['time'])[-2]

    def _insert_record_add_on(self, add_on):
        for key, value in add_on.items():
            if key not in self._record.keys():
                self._record[key] = [np.nan] * (len(self._record['time']) - 1)

            self._record[key].append(value)