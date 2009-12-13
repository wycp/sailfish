import cPickle as pickle
import os
import sys
import numpy
import sym

# Abstract class implementation, from Peter's Norvig site.
def abstract():
	import inspect
	caller = inspect.getouterframes(inspect.currentframe())[1][3]
	raise NotImplementedError(caller + ' must be implemented in subclass')

#
# Boundary conditions
#
class LBMBC(object):
	def __init__(self, name, midgrid=False, local=True, wet_walls=False):
		self.name = name
		self.midgrid = midgrid
		self.local = local
		self.wet_walls = wet_walls

SUPPORTED_BCS = [LBMBC('fullbb', midgrid=True),
				 LBMBC('halfbb', midgrid=True),
				 LBMBC('zouhe', midgrid=False, wet_walls=True)
				 ]
BCS_MAP = dict((x.name, x) for x in SUPPORTED_BCS)

class LBMGeo(object):
	"""Abstract class for the LBM geometry."""

	NODE_FLUID = 0
	NODE_WALL = 1
	NODE_VELOCITY = 2
	NODE_PRESSURE = 3

	# Constants to specify node orientation.  This needs to match the order
	# in sym.basis.
	NODE_DIR_E = 0
	NODE_DIR_N = 1
	NODE_DIR_W = 2
	NODE_DIR_S = 3
	NODE_DIR_NE = 4
	NODE_DIR_NW = 5
	NODE_DIR_SW = 6
	NODE_DIR_SE = 7
	NODE_DIR_OTHER = 8

	NODE_TYPE_MASK = 0xfffffff0
	NODE_ORIENTATION_SHIFT = 4
	NODE_ORIENTATION_MASK = 0xf

	@classmethod
	def _encode_node(cls, orientation, type):
		"""Encode a node entry for the map of nodes.

		Args:
		  orientation: node orientation
		  type: node type

		Returns:
		  node code
		"""
		return orientation | (type << cls.NODE_ORIENTATION_SHIFT)

	@classmethod
	def _decode_node(cls, code):
		"""Decode an entry from the map of nodes.

		Args:
		  code: node code from the map

		Returns:
		  tuple of orientation, type
		"""
		return (code & cls.NODE_ORIENTATION_MASK,
			    (code & cls.NODE_TYPE_MASK) >> cls.NODE_ORIENTATION_SHIFT)

	@classmethod
	def map_to_node_type(cls, node_map):
		"""Convert a node map into an array of node types.

		This is used primarily for visualization, where node orientation is
		irrelevant and only node types matter.
		"""
		return ((node_map & cls.NODE_TYPE_MASK) >> cls.NODE_ORIENTATION_SHIFT)

	def __init__(self, shape, options, float, backend, save_cache=True, use_cache=True):
		self._params = None
		self.dim = len(shape)
		self.shape = shape
		self.options = options
		self.backend = backend
		self.map = numpy.zeros(shape, numpy.int32)
		self.gpu_map = backend.alloc_buf(like=self.map)
		self.float = float
		self.save_cache = save_cache
		self.use_cache = use_cache
		self.reset()

		# Cache for equilibrium distributions.  Sympy numerical evaluation
		# is expensive, so we try to avoid unnecessary recomputations.
		self.feq_cache = {}

	def _get_state(self):
		rdict = {
			'_vel_map': self._vel_map,
			'_pressure_map': self._pressure_map,
			'map': self.map,
			'_params': self._params,
		}
		return rdict

	def _set_state(self, rdict):
		for k, v in rdict.iteritems():
			setattr(self, k, v)
		self._update_map()

	@property
	def dx(self):
		return 1.0/min(self.shape)

	def _define_nodes(self):
		"""Define the types of all nodes in the simulation domain.

		Subclasses need to override this method to specify the geometry to be used
		in the simulation.
		"""
		abstract

	def init_dist(self, dist):
		"""Initialize the particle distributions in the whole simulation domain.

		Subclasses need to override this method to provide initial conditions for
		the simulation.
		"""
		abstract

	def get_reynolds(self, viscosity):
		"""Return the Reynolds number for this geometry."""
		abstract

	def get_defines(self):
		abstract

	@property
	def cache_file(self):
		return '.sailfish_%s_%d_%s_%s' % (
				os.path.basename(sys.argv[0]), self.dim,
				'-'.join(map(str, self.shape)), str(self.float().dtype))

	def reset(self):
		"""Perform a full reset of the geometry."""

		if self.use_cache and os.path.exists(self.cache_file):
			with open(self.cache_file, 'r') as f:
				self._set_state(pickle.load(f))
			return

		self._vel_map = {}
		self._pressure_map = {}
		self.map = numpy.zeros(tuple(reversed(self.shape)), numpy.int32)
		self._define_nodes()
		self._postprocess_nodes()
		a = self.params

		if self.save_cache:
			with open(self.cache_file, 'w') as f:
				pickle.dump(self._get_state(), f, pickle.HIGHEST_PROTOCOL)

	def _get_map(self, location):
		"""Get a node map entry.

		Args:
		  location: a tuple specifying the node location
		"""
		return self.map[tuple(reversed(location))]

	def _set_map(self, location, value):
		"""Set a node map entry.

		Args:
		  location: a tuple specifying the node location
		  value: the node code as returned by _encode_node()
		"""
		self.map[tuple(reversed(location))] = value

	def _update_map(self):
		"""Copy the node map to the compute unit."""
		self.backend.to_buf(self.gpu_map, self.map)

	def set_geo(self, location, type, val=None, update=False):
		"""Set the type of a grid node.

		Args:
		  location: location of the node
		  type: type of the node, one of the NODE_* constants
		  val: optional argument for the node, e.g. the value of velocity or pressure
		  update: whether to automatically update the geometry for the simulation
		"""
		self._set_map(location, numpy.int32(type))

		if val is not None:
			if type == self.NODE_VELOCITY:
				if len(val) == sym.GRID.dim:
					self._vel_map.setdefault(val, []).append(location)
				else:
					raise ValueError('Invalid velocity specified.')
			elif type == self.NODE_PRESSURE:
				self._pressure_map.setdefault(val, []).append(location)

		if update:
			self._postprocess_nodes(nodes=[location])
			self._update_map()

	def mask_array_by_fluid(self, array):
		if self.get_bc().wet_walls:
			return array
		mask = (self.map_to_node_type(self.map) == self.NODE_WALL)
		return numpy.ma.array(array, mask=mask)

	def fill_dist(self, location, dist, target=None):
		"""Fill the whole simulation domain with distributions from a specific node.

		Args:
		  location: location of the node, a n-tuple.  The location can also be a row,
		    in which case the coordinate spanning the row should be set to slice(None).
		  dist: the distribution array
		  target: if not None, a n-tuple representing the area to which the data from
		    the specified node is to be propagated
		"""
		loc = list(reversed(location))
		out = dist

		if target is None:
			tg = [slice(None)] * len(location)

			# In order for numpy's array broadcasting to work correctly, the dimensions
			# indexed with the ':' operator must be to the right of any dimensions with
			# a specific numerical index, i.e. t[:,:,:] = b[0,0,:] works, but
			# t[:,:,:] = b[0,:,0] does not.
			#
			# To work around this, we swap the axes of the dist array to get the correct
			# form for broadcasting.
			start = None
			for	i in reversed(range(0, len(loc))):
				if start is None and loc[i] != slice(None):
					start = i
				elif start is not None and loc[i] == slice(None):
					t = loc[start]
					loc[start] = loc[i]
					loc[i] = t

					# +1 as the first dimension is for different velocity directions.
					out = numpy.swapaxes(out, i+1, start+1)
					start -= 1
		else:
			tg = list(reversed(target))

		for i in range(0, len(sym.GRID.basis)):
			addr = tuple([i] + loc)
			dest = tuple([i] + tg)
			out[dest] = out[addr]

	def velocity_to_dist(self, location, velocity, dist, rho=1.0):
		"""Set the distributions for a node so that the fluid there has a
		specific velocity.

		Args:
		  velocity: velocity to set, a n-tuple
		  dist: the distribution array
		  location: location of the node, a n-tuple
		"""
		if (rho, velocity) not in self.feq_cache:
			vals = []
			self.feq_cache[(rho, velocity)] = map(self.float, sym.eval_bgk_equilibrium(velocity, rho))

		for i, val in enumerate(self.feq_cache[(rho, velocity)]):
			dist[i][tuple(reversed(location))] = val

	def _postprocess_nodes(self, nodes=None):
		"""Detect types of wall nodes and mark them appropriately.

		Args:
		  nodes: optional iterable of locations to postprocess
		"""
		abstract

	@property
	def params(self):
		if self._params is not None:
			return self._params

		ret = []
		i = 0
		for v, pos_list in self._vel_map.iteritems():
			ret.extend(v)
			for location in pos_list:
				orientation, type = self._decode_node(self._get_map(location))
				self._set_map(location, self._encode_node(orientation, type + i))

			i += 1

		i -= 1

		for p, pos_list in self._pressure_map.iteritems():
			ret.append(p)
			for location in pos_list:
				orientation, type = self._decode_node(self._get_map(location))
				self._set_map(location, self._encode_node(orientation, type + i))

			i += 1

		self._update_map()
		self._params = ret
		return ret

	def get_bc(self):
		return BCS_MAP[self.options.boundary]

	def init_dist(self, dist):
		abstract

class LBMGeo2D(LBMGeo):
	def __init__(self, shape, *args, **kwargs):
		self.lat_w, self.lat_h = shape
		LBMGeo.__init__(self, shape, *args, **kwargs)

	def get_defines(self):
		return {'geo_fluid': self.NODE_FLUID,
				'geo_wall': self.NODE_WALL,
				'geo_wall_e': self.NODE_DIR_E,
				'geo_wall_w': self.NODE_DIR_W,
				'geo_wall_s': self.NODE_DIR_S,
				'geo_wall_n': self.NODE_DIR_N,
				'geo_wall_ne': self.NODE_DIR_NE,
				'geo_wall_se': self.NODE_DIR_SE,
				'geo_wall_nw': self.NODE_DIR_NW,
				'geo_wall_sw': self.NODE_DIR_SW,
				'geo_dir_other': self.NODE_DIR_OTHER,
				'geo_bcv': self.NODE_VELOCITY,
				'geo_bcp': self.NODE_PRESSURE + len(self._vel_map) - 1,
				'geo_orientation_mask': self.NODE_ORIENTATION_MASK,
				'geo_orientation_shift': self.NODE_ORIENTATION_SHIFT,
				}

	def _postprocess_nodes(self, nodes=None):
		lat_w, lat_h = self.shape

		if nodes is None:
			nodes_ = ((x, y) for x in range(0, lat_w) for y in range(0, lat_h))
		else:
			nodes_ = nodes

		for x, y in nodes_:
			if self.map[y][x] != self.NODE_FLUID:
				# If the bool corresponding to a specific direction is True, the
				# distributions in this direction are undefined.
				north = y < lat_h-1 and self.map[y+1][x] == self.NODE_FLUID
				south = y > 0 and self.map[y-1][x] == self.NODE_FLUID
				west  = x > 0 and self.map[y][x-1] == self.NODE_FLUID
				east  = x < lat_w-1 and self.map[y][x+1] == self.NODE_FLUID

				# Walls aligned with the grid.
				if north and not west and not east:
					self.map[y][x] = self._encode_node(self.NODE_DIR_N, self.map[y][x])
				elif south and not west and not east:
					self.map[y][x] = self._encode_node(self.NODE_DIR_S, self.map[y][x])
				elif west and not south and not north:
					self.map[y][x] = self._encode_node(self.NODE_DIR_W, self.map[y][x])
				elif east and not south and not north:
					self.map[y][x] = self._encode_node(self.NODE_DIR_E, self.map[y][x])
				# Corners.
				elif y > 0 and x > 0 and self.map[y-1][x-1] == self.NODE_FLUID:
					self.map[y][x] = self._encode_node(self.NODE_DIR_SW, self.map[y][x])
				elif y > 0 and x < lat_w-1 and self.map[y-1][x+1] == self.NODE_FLUID:
					self.map[y][x] = self._encode_node(self.NODE_DIR_SE, self.map[y][x])
				elif y < lat_h-1 and x > 0 and self.map[y+1][x-1] == self.NODE_FLUID:
					self.map[y][x] = self._encode_node(self.NODE_DIR_NW, self.map[y][x])
				elif y < lat_h-1 and x < lat_w-1 and self.map[y+1][x+1] == self.NODE_FLUID:
					self.map[y][x] = self._encode_node(self.NODE_DIR_NE, self.map[y][x])
				else:
					self.map[y][x] = self._encode_node(self.NODE_DIR_OTHER, self.map[y][x])

class LBMGeo3D(LBMGeo):
	def __init__(self, shape, *args, **kwargs):
		self.lat_w, self.lat_h, self.lat_d = shape
		LBMGeo.__init__(self, shape, *args, **kwargs)

	def get_defines(self):
		return {'geo_fluid': self.NODE_FLUID,
				'geo_wall': self.NODE_WALL,
				'geo_bcv': self.NODE_VELOCITY,
				'geo_bcp': self.NODE_PRESSURE + len(self._vel_map) - 1,
				'geo_orientation_mask': self.NODE_ORIENTATION_MASK,
				'geo_orientation_shift': self.NODE_ORIENTATION_SHIFT,
				'geo_dir_other': self.NODE_DIR_OTHER,
				}

	def _postprocess_nodes(self, nodes=None):
		lat_w, lat_h, lat_d = self.shape

		if nodes is None:
			nodes_ = ((x, y, z) for x in range(0, lat_w) for y in range(0, lat_h) for z in range(0, lat_d))
		else:
			nodes_ = nodes

		# FIXME: Eventually, we will need to postprocess nodes in 3D grids as well.
		for loc in nodes_:
			cnode_type = self._get_map(loc)

			if cnode_type != self.NODE_FLUID:
				self._set_map(loc, self._encode_node(self.NODE_DIR_OTHER, cnode_type))

