import io
import logging
import os
import shutil
import tempfile

import imageio.v3 as iio

from generated.formats.dds import DdsFile
from generated.formats.dds.enums.DxgiFormat import DxgiFormat
from generated.formats.ovl.versions import *
from generated.formats.tex.compounds.TexHeader import TexHeader
from generated.formats.tex.compounds.TexturestreamHeader import TexturestreamHeader
from modules.formats.BaseFormat import MemStructLoader

from ovl_util import texconv, imarray

logging.getLogger('PIL').setLevel(logging.WARNING)


def align_to(width, comp, alignment=64):
	"""Return input padded to the next closer multiple of alignment"""
	# get bpp from compression type
	if "BC1" in comp or "BC4" in comp:
		alignment *= 2
	# print("alignment",alignment)
	m = width % alignment
	if m:
		return width + alignment - m
	return width


class TexturestreamLoader(MemStructLoader):
	extension = ".texturestream"
	can_extract = False
	target_class = TexturestreamHeader

	def create(self, file_path):
		self.header = self.target_class(self.context)
		if is_jwe2(self.ovl):
			self.header.lod_index = int(self.basename[-1])
		self.write_memory_data()
		# data entry, assign buffer
		self.create_data_entry((b"", ))


class DdsLoader(MemStructLoader):
	target_class = TexHeader
	extension = ".tex"
	temp_extensions = (".dds", )

	def link_streams(self):
		"""Collect other loaders"""
		self._link_streams(f"{self.basename}_lod{lod_i}.texturestream" for lod_i in range(3))

	def increment_buffers(self, loader, buffer_i):
		"""Linearly increments buffer indices for games that need it"""
		# create increasing buffer indices for PZ (still needed 22-05-10), JWE1
		if not is_jwe2(self.ovl):
			for buff in loader.data_entry.buffers:
				buff.index = buffer_i
				buffer_i += 1
		return buffer_i

	def create(self, file_path):
		in_dir, name_ext, basename, ext = self.get_names(file_path)
		super().create(file_path)
		logging.debug(f"Creating image {name_ext}")
		# there's one empty buffer at the end!
		buffers = [b"" for _ in range(self.header.stream_count + 1)]
		# decide where to store the buffers
		static_lods = 2
		streamed_lods = len(buffers) - static_lods
		# logging.debug(f"buffers: {len(buffers)} streamed lods: {streamed_lods}")
		buffer_i = 0
		# generate ovs and lod names - highly idiosyncratic
		# checked for PZ, JWE2, PC
		if streamed_lods == 0:
			indices = ()
		elif streamed_lods == 1:
			# 1 lod: lod0 -> L1
			indices = ((0, 1),)
		elif streamed_lods == 2:
			# 2 lods: lod0 -> L0, lod1 -> L1
			indices = ((0, 0), (1, 1), )
		else:
			raise IndexError(f"Don't know how to handle more than 2 streams for {name_ext}")
		for lod_i, ovs_i in indices:
			ovs_name = f"Textures_L{ovs_i}"
			# create texturestream file - dummy_dir is ignored
			texstream_loader = self.ovl.create_file(f"dummy_dir/{basename}_lod{lod_i}.texturestream", ovs_name=ovs_name)
			self.streams.append(texstream_loader)
			buffer_i = self.increment_buffers(texstream_loader, buffer_i)
		self.create_data_entry(buffers[streamed_lods:])
		self.increment_buffers(self, buffer_i)
		# ready, now inject
		self.load_image(file_path)

	def load_image(self, tex_path):
		# logging.debug(f"Loading image {tex_path}")
		in_dir, name_ext, basename, ext = self.get_names(tex_path)
		# create tmp folder in which all conversions are stored
		tmp_dir = tempfile.mkdtemp("-cobra-tools")
		size_info = self.get_tex_structs()
		tiles = self.get_tiles(size_info)
		# load all DDS files we need
		dds_files = []
		for tile_i, tile_name in zip(tiles, self.get_tile_names(tiles, basename)):
			bare_path = os.path.join(in_dir, tile_name)
			dds_path = f"{bare_path}.dds"
			# prioritize dds files if they exist
			if os.path.isfile(dds_path):
				dds_file = self.load_dds(dds_path)
			else:
				# try to reassemble a flat PNG for this tile, and then convert it to DDS
				png_path = imarray.join_png(bare_path, tmp_dir, self.compression_name)
				dds_file = self.load_png(png_path, tmp_dir)
			dds_files.append(dds_file)
		# pack the different tiles into the tex buffer, pad the mips
		# create list of bytes for each buffer
		tex_buffers = self.header.buffer_infos.data
		if is_pc(self.ovl):
			# todo PC array textures
			buffer_bytes = dds_files[0].pack_mips_pc(tex_buffers)
		else:
			logging.info("Packing mip maps")
			dds_mips = [dds.get_packed_mips(size_info.mip_maps) for dds in dds_files]
			with io.BytesIO() as tex:
				# write the packed tex buffer: for each mip level, write all its tiles consecutively
				for mip_level in zip(*dds_mips):
					for tile in mip_level:
						tex.write(tile)
				packed = tex.getvalue()
			# slice packed bytes according to tex header buffer specifications
			buffer_bytes = [packed[b.offset: b.offset + b.size] for b in tex_buffers]
		# set data on the buffers
		for buffer_entry, b_slice in zip(self.get_sorted_streams(), buffer_bytes):
			buffer_entry.update_data(b_slice)
		# fix as we don't use the data.update_data api here
		for data_entry in self.get_sorted_datas():
			data_entry.size_1 = 0
			data_entry.size_2 = sum(buffer.size for buffer in data_entry.buffers)
		# cleanup tmp folder
		shutil.rmtree(tmp_dir)

	def get_names(self, file_path):
		assert file_path == os.path.normpath(file_path)
		in_dir, name_ext = os.path.split(file_path)
		basename, ext = os.path.splitext(name_ext.lower())
		return in_dir, name_ext, basename, ext

	def load_png(self, png_path, tmp_dir):
		logging.info(f"Loading PNG {png_path}")
		# convert the png into a dds
		size_info = self.get_tex_structs()
		compression = self.header.compression_type.name
		# need to check the png dimensions here because texconv writes weird things to the dds header
		self.ensure_size_match(png_path, size_info)
		# compress and generate mips if needed
		dds_file_path = texconv.png_to_dds(png_path, tmp_dir, codec=compression, mips=size_info.num_mips)
		# inject the dds generated by texconv
		return self.load_dds(dds_file_path)

	def load_dds(self, dds_path):
		logging.info(f"Loading DDS {dds_path}")
		# load dds
		dds_file = DdsFile()
		dds_file.load(dds_path)
		# print(dds_file)
		return dds_file

	def get_sorted_datas(self):
		# lod0 | lod1 | static
		return [loader.data_entry for loader in sorted(self.streams, key=lambda l: l.name)] + [self.data_entry, ]

	def get_sorted_streams(self):
		# PZ assigns the buffer index for the complete struct 0 | 1 | 2, 3
		# from JWE2, buffer index for streams is 0 | 0 | 0, 1
		# the last buffer is always 0 bytes
		# seen 1 buffer per stream
		# seen 2 buffer for static
		return [b for data_entry in self.get_sorted_datas() for b in data_entry.buffers]

	def get_tex_structs(self):
		if is_dla(self.ovl):
			return self.header
		if self.ovl.version < 19:
			# this corresponds to a stripped down size_info
			return self.header.buffer_infos.data[0]
		else:
			return self.header.size_info.data.data

	def get_tile_names(self, tiles, basename):
		tile_names = []
		for tile_i in tiles:
			# when there are tiles, add a tile infix
			infix = f"_[{tile_i:02}]" if len(tiles) > 1 else ""
			tile_name = f"{basename}{infix}"
			tile_names.append(tile_name)
		return tile_names

	def extract(self, out_dir):
		out_files = list(super().extract(out_dir))
		tex_name = self.name
		basename = os.path.splitext(tex_name)[0]
		logging.info(f"Writing {tex_name}")

		# get joined output buffer
		buffer_data = b"".join([buffer.data for buffer in self.get_sorted_streams()])
		size_info = self.get_tex_structs()

		dds_file = DdsFile()
		dds_file.width = size_info.width
		dds_file.height = size_info.height
		dds_file.mipmap_count = size_info.num_mips
		dds_file.dx_10.num_tiles = 1
		if hasattr(size_info, "depth") and size_info.depth:
			dds_file.depth = size_info.depth

		compression_type = DxgiFormat[self.compression_name]

		# header attribs
		if not is_ztuac(self.ovl):
			dds_file.width = align_to(dds_file.width, self.compression_name)

		# set compression
		dds_file.dx_10.dxgi_format = compression_type

		tiles = self.get_tiles(size_info)
		# export all tiles
		for tile_i, tile_name in zip(tiles, self.get_tile_names(tiles, basename)):
			# get the mip mapped data for just this tile from the packed tex buffer
			if hasattr(size_info, "num_tiles"):
				dds_file.dx_10.num_tiles = size_info.num_tiles
			else:
				# DLA has no num_tiles
				dds_file.dx_10.num_tiles = 1
			if is_dla(self.ovl) or is_ztuac(self.ovl) or is_pc(self.ovl):
				# not sure how / if texture arrays are packed for PC - this works for flat textures
				tile_data = buffer_data
			else:
				tile_data = dds_file.unpack_mips(size_info.mip_maps, tile_i, buffer_data)
			dds_file.dx_10.num_tiles = 1
			dds_file.buffer = tile_data
			dds_file.linear_size = len(buffer_data)
			dds_path = out_dir(f"{tile_name}.dds")
			# write dds
			dds_file.save(dds_path)
			out_files.append(dds_path)
			try:
				# convert the dds to png
				png_path = texconv.dds_to_png(dds_path)
				# postprocessing of the png
				if os.path.isfile(png_path):
					out_files.extend(imarray.split_png(png_path, self.ovl, self.compression_name))
			except:
				logging.exception(f"Postprocessing of {dds_path} failed!")
		return out_files

	@property
	def compression_name(self):
		name = self.header.compression_type.name
		# account for aliases
		if name.endswith(("_B", "_C")):
			name = name[:-2]
		return name

	def get_tiles(self, size_info):
		if hasattr(size_info, "num_tiles") and size_info.num_tiles:
			tiles = list(range(size_info.num_tiles))
		else:
			tiles = (0,)
		return tiles

	def ensure_size_match(self, png_file_path, size_info):
		"""Check that DDS files have the same basic size"""
		try:
			png_width, png_height = iio.immeta(png_file_path)["shape"]
		except:
			logging.exception(f"Could not get dimensions of {png_file_path}")
			raise
		tex_h = size_info.height
		tex_w = size_info.width
		if hasattr(size_info, "tex_d"):
			tex_d = size_info.depth
		else:
			tex_d = 1
		tex_a = size_info.num_tiles
		tex_w = align_to(tex_w, self.header.compression_type.name)
		if png_width * png_height != tex_h * tex_w:
			raise AttributeError(
				f"Dimensions do not match for {self.name}!\n"
				f"Dimensions: height x width x depth [num_tiles]\n"
				f".tex file: {tex_h} x {tex_w} x {tex_d} [{tex_a}]\n"
				f".png file: {png_height} x {png_width}\n\n"
				f"Make the textures' dimensions match and try again!")
