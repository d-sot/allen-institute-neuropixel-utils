import h5py
import zarr
import numpy as np
from urllib.parse import urlparse, urlunparse
import numcodecs
import fsspec
import hdf5plugin
from typing import Union
import os
from pathlib import Path
from collections.abc import MutableMapping
from pathlib import PurePosixPath
from zarr.util import json_dumps, json_loads


class NWBZarr(object):
    """ class to create zarr structure for reading NWB files """

    def __init__(self, nwbfile: str = None, nwbgroup: str = None, nwbfile_mode: str = 'r',
                 store: Union[MutableMapping, str, Path] = None, store_path: str = None,
                 allow_changing_string_types: bool = False, store_mode: str = 'a', consolidate_metadata: bool = True,
                 metadata_key: str = '.zmetadata', LRU: bool = True, LRU_max_size: int = 2**30):
        """
        Args:
            nwbfile:                     str, path of NWB file to be read by zarr
            nwbgroup:                    str, hdf5 group in NWB file to be read by zarr
                                         along with its children. default is the root group.
            nwbfile_mode                 str, subset of h5py file access modes, nwbfile must exist
                                         'r'          readonly, default 'r'
                                         'r+'         read and write, allows changing the hdf5 file if
                                                      flag allow_changing_string_type is True,
                                                      otherwise switches to 'r' mode
            store:                       collections.abc.MutableMapping or str, zarr store.
                                         if string path is passed, zarr.DirectoryStore
                                         is created at the given path, if None, zarr.MemoryStore is used
            store_mode:                  store data access mode, default 'a'
                                         'r'          readonly, compatible zarr hierarchy should
                                                      already exist in the passed store
                                         'r+'         read and write, return error if file does not exist,
                                                      for updating zarr hierarchy
                                         'w'          create store, remove data if it exists
                                         'w-' or 'x'  create store, fail if exists
                                         'a'          read and write, create if it does not exist, default 'r'
                                         If consolidate_metadata is True, only 'r' and 'r+' are allowed
            store_path:                  string, path in store
            allow_changing_string_types: bool, if True, change variable string types if not compatible with zarr,
                                         if False, copy string datasets to a zarr store
            consolidate_metadata:        bool, flag whether to create a consolidated metadata file
            metadata_key:                string, name of consolidated metadata file,
                                         ignored if consolidate_metadata = False
            LRU:                         bool, if store is not already zarr.LRUStoreCache, add
                                         a zarr.LRUStoreCache store layer on top of currently used store
            LRU_max_size:                int, maximum zarr.LRUStoreCache cache size, only used
                                         if store is zarr.LRUStoreCache, or LRU argument is True
        """
        # Verify arguments
        if nwbfile_mode not in ('r', 'r+'):
            raise ValueError("nwbfile_mode must be 'r' or 'r+'")
        if not isinstance(allow_changing_string_types, bool):
            raise TypeError(f"Expected bool for allow_changing_string_types, recieved {type(allow_changing_string_types)}")
        self.allow_changing_string_types = allow_changing_string_types
        self.nwbfile_mode = nwbfile_mode
        if self.nwbfile_mode == 'r+' and not self.allow_changing_string_types:
            self.nwbfile_mode = 'r'

        # Access NWB file
        self.file = h5py.File(nwbfile, mode=self.nwbfile_mode)
        if nwbgroup and not isinstance(nwbgroup, str):
            raise TypeError(f"Expected str for nwbgroup, recieved {type(nwbgroup)}")
        self.nwbgroup = nwbgroup
        self.group = self.file[self.nwbgroup] if self.nwbgroup else self.file

        # Verify arguments
        if not isinstance(LRU, bool):
            raise TypeError(f"Expected bool for LRU, recieved {type(LRU)}")
        self.LRU = LRU
        if not isinstance(LRU_max_size, int):
            raise TypeError(f"Expected int for LRU_max_size, recieved {type(LRU_max_size)}")
        self.LRU_max_size = LRU_max_size

        if not isinstance(consolidate_metadata, bool):
            raise TypeError(f"Expected bool for consolidate_metadata, recieved {type(consolidate_metadata)}")
        self.consolidate_metadata = consolidate_metadata
        if not isinstance(metadata_key, str):
            raise TypeError(f"Expected str for metadata_key, recieved {type(metadata_key)}")
        self.metadata_key = metadata_key

        # store, store_path, and store_mode are passed through to zarr
        self.store_path = store_path
        self.store_mode = store_mode
        if store is not None and LRU is True and not isinstance(store, zarr.LRUStoreCache):
            self.store = zarr.LRUStoreCache(store, max_size=self.LRU_max_size)
        else:
            self.store = store

        # create dictionary mapping hdf5 filter numbers to compatible zarr codec
        self._hdf5_regfilters_subset = {}
        self._fill_regfilters()

        # dictionary to hold addresses of hdf5 objects in file
        self._address_dict = {}

        # create zarr format hierarchy for datasets and attributes compatible with NWB file,
        # dataset contents are not copied, unless allow_changing_string_type is False or
        # nwbfile_mode == 'r', and NWBFile contains variable-length strings

        # FileChunkStore requires uri
        if isinstance(nwbfile, str):
            self.uri = nwbfile
        else:
            self.uri = nwbfile.path

        if self.consolidate_metadata:
            # TO DO correct store_mode
            if self.store_mode == 'r':
                store_mode_cons = self.store_mode
            else:
                store_mode_cons = 'r+'
                self.nwb_zgroup = zarr.group(self.store, overwrite=True, path=self.store_path)
                if self.store is None:
                    self.store = self.nwb_zgroup.store
                self.create_zarr_hierarchy(self.group, self.nwb_zgroup)
                zarr.convenience.consolidate_metadata(self.store, metadata_key=self.metadata_key)
            if isinstance(nwbfile, str):
                self.fsspec_file = fsspec.open(nwbfile, mode='rb')
                chunk_store = FileChunkStore(self.store, chunk_source=self.fsspec_file.open())
            else:
                self.fsspec_file = nwbfile
                chunk_store = FileChunkStore(self.store, chunk_source=nwbfile)

            if LRU is True and not isinstance(chunk_store, zarr.LRUStoreCache):
                chunk_store = zarr.LRUStoreCache(chunk_store, max_size=self.LRU_max_size)

            self.nwb_zgroup = zarr.convenience.open_consolidated(self.store, metadata_key=self.metadata_key,
                                                                 mode=store_mode_cons, chunk_store=chunk_store,
                                                                 path=self.store_path)
        else:
            # TO DO correct store_mode
            if self.store_mode == 'r':
                store_mode_cons = self.store_mode
            else:
                store_mode_cons = 'r+'
                self.nwb_zgroup = zarr.group(self.store, overwrite=True, path=self.store_path)
                if self.store is None:
                    self.store = self.nwb_zgroup.store
                self.create_zarr_hierarchy(self.group, self.nwb_zgroup)
            if isinstance(nwbfile, str):
                self.fsspec_file = fsspec.open(nwbfile, mode='rb')
                chunk_store = FileChunkStore(self.store, chunk_source=self.fsspec_file.open())
            else:
                self.fsspec_file = nwbfile
                chunk_store = FileChunkStore(self.store, chunk_source=nwbfile)
            if LRU is True and not isinstance(chunk_store, zarr.LRUStoreCache):
                chunk_store = zarr.LRUStoreCache(chunk_store, max_size=self.LRU_max_size)

            self.nwb_zgroup = zarr.open_group(self.store, mode=store_mode_cons, path=self.store_path, chunk_store=chunk_store)

    def _fill_regfilters(self):

        # h5py.h5z.FILTER_DEFLATE == 1
        self._hdf5_regfilters_subset[1] = numcodecs.GZip

        # h5py.h5z.FILTER_SHUFFLE == 2
        self._hdf5_regfilters_subset[2] = None

        # h5py.h5z.FILTER_FLETCHER32 == 3
        self._hdf5_regfilters_subset[3] = None

        # h5py.h5z.FILTER_SZIP == 4
        self._hdf5_regfilters_subset[4] = None

        # h5py.h5z.FILTER_SCALEOFFSET == 6
        self._hdf5_regfilters_subset[6] = None

        # LZO
        self._hdf5_regfilters_subset[305] = None

        # BZIP2
        self._hdf5_regfilters_subset[307] = numcodecs.BZ2

        # LZF
        self._hdf5_regfilters_subset[32000] = None

        # Blosc
        self._hdf5_regfilters_subset[32001] = numcodecs.Blosc

        # Snappy
        self._hdf5_regfilters_subset[32003] = None

        # LZ4
        self._hdf5_regfilters_subset[32004] = numcodecs.LZ4

        # bitshuffle
        self._hdf5_regfilters_subset[32008] = None

        # JPEG-LS
        self._hdf5_regfilters_subset[32012] = None

        # Zfp
        self._hdf5_regfilters_subset[32013] = None

        # Fpzip
        self._hdf5_regfilters_subset[32014] = None

        # Zstandard
        self._hdf5_regfilters_subset[32015] = numcodecs.Zstd

        # FCIDECOMP
        self._hdf5_regfilters_subset[32018] = None

    def copy_attrs_data_to_zarr_store(self, h5obj, zobj):
        """ Convert hdf5 attributes to json compatible form and create zarr attributes
        Args:
            h5obj:   hdf5 object
            zobj:    zarr object
        """

        for key, val in h5obj.attrs.items():

            # convert object references in attrs to str
            # e.g. h5py.h5r.Reference instance to "/processing/ophys/ImageSegmentation/ImagingPlane"
            if isinstance(val, h5py.h5r.Reference):
                if not val:
                    val = None
                val = self.file[val].name
            elif isinstance(val, h5py.h5r.RegionReference):
                print(f"Attribute value of type {type(val)} is not processed: Attribute {key} of object {h5obj.name}")
            elif isinstance(val, bytes):
                val = val.decode('utf-8')
            elif isinstance(val, np.bool_):
                val = np.bool(val)
            elif isinstance(val, (np.ndarray, np.number)):
                if val.dtype.kind == 'S':
                    val = np.char.decode(val, 'utf-8')
                    val = val.tolist()
                else:
                    val = val.tolist()
            try:
                zobj.attrs[key] = val
            except Exception:
                print(f"Attribute value of type {type(val)} is not processed: Attribute {key} of object {h5obj.name}")

    def storage_info(self, dset):
        if dset.shape is None:
            # Null dataset
            return dict()

        dsid = dset.id
        if dset.chunks is None:
            # get offset for Non-External datasets
            if dsid.get_offset() is None:
                return dict()
            else:
                key = (0,) * (len(dset.shape) or 1)
                return {key: {'offset': dsid.get_offset(),
                              'size': dsid.get_storage_size()}}
        else:
            # Currently, this function only gets the number of all written chunks, regardless of the dataspace.
            # HDF5 1.10.5
            num_chunks = dsid.get_num_chunks()

            if num_chunks == 0:
                return dict()

            stinfo = dict()
            chunk_size = dset.chunks
            for index in range(num_chunks):
                blob = dsid.get_chunk_info(index)
                key = tuple(
                    [a // b for a, b in zip(blob.chunk_offset, chunk_size)])
                stinfo[key] = {'offset': blob.byte_offset,
                               'size': blob.size}
            return stinfo

    def create_zarr_hierarchy(self, h5py_group, zgroup):
        """  Scan NWB file and recursively create zarr attributes, groups and dataset structures for accessing data
        Args:
          h5py_group: h5py.Group or h5py.File object where information is gathered from
          zgroup:     Zarr Group
        """

        if (not isinstance(h5py_group, h5py.File) and
            (not issubclass(self.file.get(h5py_group.name, getclass=True), h5py.Group) or
             not issubclass(self.file.get(h5py_group.name, getclass=True, getlink=True), h5py.HardLink))):
            raise TypeError(f"{h5py_group} should be a h5py.File or h5py.Group as a h5py.HardLink")

        self.copy_attrs_data_to_zarr_store(h5py_group, zgroup)

        # add hdf5 group address in file to self._address_dict
        self._address_dict[h5py.h5o.get_info(h5py_group.id).addr] = h5py_group.name

        # iterate through group members
        test_iter = [name for name in h5py_group.keys()]
        for name in test_iter:
            obj = h5py_group[name]

            # get group member's link class
            obj_linkclass = h5py_group.get(name, getclass=True, getlink=True)

            # Datasets
            # TO DO, Soft Links #
            if issubclass(h5py_group.get(name, getclass=True), h5py.Dataset):
                if issubclass(obj_linkclass, h5py.ExternalLink):
                    print(f"Dataset {obj.name} is not processed: External Link")
                    continue
                dset = obj
                if dset.dtype.kind == 'V':
                    # TO DO #
                    print(f"Dataset {dset.name} of dtype {dset.dtype.kind} is not processed")
                else:
                    # number of filters
                    dcpl = dset.id.get_create_plist()
                    nfilters = dcpl.get_nfilters()
                    if nfilters > 1:
                        # TO DO #
                        print(f"Dataset {dset.name} with multiple filters is not processed")
                        continue
                    elif nfilters == 1:
                        # get first filter information
                        filter_tuple = dset.id.get_create_plist().get_filter(0)
                        filter_code = filter_tuple[0]
                        if filter_code in self._hdf5_regfilters_subset and self._hdf5_regfilters_subset[filter_code] is not None:
                            # TO DO
                            compression = self._hdf5_regfilters_subset[filter_code](level=filter_tuple[2])
                        else:
                            print(f"Dataset {dset.name} with compression filter {filter_tuple[3]}, hdf5 filter number {filter_tuple[0]} is not processed:\
                                    no compatible zarr codec")
                            continue
                    else:
                        compression = None

                    info = self.storage_info(dset)

                    # TO DO compound dtype #
                    if dset.dtype.names:
                        # TO DO #
                        print(f"Dataset {dset.name} is not processed: compound dtype")
                        continue
                    # variable-length Datasets
                    if h5py.check_vlen_dtype(dset.dtype):
                        if not h5py.check_string_dtype(dset.dtype):
                            # TO DO #
                            print(f"Dataset {dset.name} is not processed: Variable-length dataset, not string")
                            continue
                        else:
                            vlen_stringarr = dset[()]
                            if dset.shape == ():
                                string_lengths_ = len(vlen_stringarr)
                                length_max = string_lengths_
                            else:
                                length_max = max(len(el) for el in vlen_stringarr.flatten())
                            dt_fixedlen = f'|S{length_max}'

                            if self.allow_changing_string_types and self.nwbfile_mode == 'r+':
                                # TO DO, delete moved datasets
                                affix_ = '_fixedlen~'
                                if name.endswith(affix_):
                                    continue

                                dset_name = dset.name
                                h5py_group.file.move(dset_name, dset_name+affix_)
                                dsetf = self.file.create_dataset(dset_name, shape=dset.shape,
                                                                 dtype=dt_fixedlen,
                                                                 compression=compression,
                                                                 compression_opts=dset.compression_opts,
                                                                 maxshape=dset.maxshape,
                                                                 fillvalue=dset.fillvalue)
                                if dsetf.shape == ():
                                    if isinstance(vlen_stringarr, bytes):
                                        dsetf[...] = vlen_stringarr
                                    else:
                                        dsetf[...] = vlen_stringarr.encode('utf-8')
                                else:
                                    dsetf[...] = vlen_stringarr.astype(dt_fixedlen)

                                info = self.storage_info(dsetf)

                                zarray = zgroup.create_dataset(dsetf.name, shape=dsetf.shape,
                                                               dtype=dsetf.dtype,
                                                               chunks=dsetf.chunks or False,
                                                               fill_value=dsetf.fillvalue,
                                                               compression=compression,
                                                               overwrite=True)
                            else:
                                # TO DO #
                                print(f"Dataset {dset.name} is not processed. Info: Variable-length dataset is not copied")
                                continue

                    elif dset.dtype.hasobject:
                        # TO DO #
                        print(f"Dataset {dset.name} is not processed: Dataset: {obj}")
                        continue
                    else:
                        zarray = zgroup.create_dataset(obj.name, shape=obj.shape,
                                                       dtype=obj.dtype,
                                                       chunks=obj.chunks or False,
                                                       fill_value=obj.fillvalue,
                                                       compression=compression,
                                                       overwrite=True)

                    self.copy_attrs_data_to_zarr_store(dset, zarray)

                    # Store chunk location metadata...
                    if info:
                        info['source'] = {'uri': self.uri,
                                          'array_name': obj.name}
                        FileChunkStore.chunks_info(zarray, info)

            # Groups
            elif (issubclass(h5py_group.get(name, getclass=True), h5py.Group) and
                  not issubclass(obj_linkclass, h5py.SoftLink)):
                if issubclass(obj_linkclass, h5py.ExternalLink):
                    print(f"Group {obj.name} is not processed: External Link")
                    continue
                group_ = obj
                zgroup_ = self.nwb_zgroup.create_group(group_.name, overwrite=True)
                self.create_zarr_hierarchy(group_, zgroup_)

            # Groups, Soft Link
            elif (issubclass(h5py_group.get(name, getclass=True), h5py.Group) and
                  issubclass(obj_linkclass, h5py.SoftLink)):
                group_ = obj
                zgroup_ = self.nwb_zgroup.create_group(group_.name, overwrite=True)


# from zarr.storage: #
chunks_meta_key = '.zchunkstore'


def _path_to_prefix(path):
    # assume path already normalized
    if path:
        prefix = path + '/'
    else:
        prefix = ''
    return prefix


class FileChunkStore(MutableMapping):
    """A file as a chunk store.
    Zarr array chunks are all in a single file.
    Parameters
    ----------
    store : MutableMapping
        Store for file chunk location metadata.
    chunk_source : file-like object
        Source (file) containing chunk bytes. Must be seekable and readable.
    """

    def __init__(self, store, chunk_source):
        self._store = store
        if not (chunk_source.seekable and chunk_source.readable):
            raise TypeError(f'{chunk_source}: chunk source is not '
                            'seekable and readable')
        self._source = chunk_source

    @property
    def store(self):
        """MutableMapping store for file chunk information"""
        return self._store

    @store.setter
    def store(self, new_store):
        """Set the new store for file chunk location metadata."""
        self._store = new_store

    @property
    def source(self):
        """The file object where chunks are stored."""
        return self._source

    @staticmethod
    def chunks_info(zarray, chunks_loc):
        """Store chunks location information for a Zarr array.
        Parameters
        ----------
        zarray : zarr.core.Array
            Zarr array that will use the chunk data.
        chunks_loc : dict
            File storage information for the chunks belonging to the Zarr array.
        """
        if 'source' not in chunks_loc:
            raise ValueError('Chunk source information missing')
        if any([k not in chunks_loc['source'] for k in ('uri', 'array_name')]):
            raise ValueError(
                f'{chunks_loc["source"]}: Chunk source information incomplete')

        key = _path_to_prefix(zarray.path) + chunks_meta_key
        chunks_meta = dict()
        for k, v in chunks_loc.items():
            if k != 'source':
                k = zarray._chunk_key(k)
                if any([a not in v for a in ('offset', 'size')]):
                    raise ValueError(
                        f'{k}: Incomplete chunk location information')
            chunks_meta[k] = v

        # Store Zarr array chunk location metadata...
        zarray.store[key] = json_dumps(chunks_meta)

    def _get_chunkstore_key(self, chunk_key):
        return str(PurePosixPath(chunk_key).parent / chunks_meta_key)

    def _ensure_dict(self, obj):
        if isinstance(obj, bytes):
            return json_loads(obj)
        else:
            return obj

    def __getitem__(self, chunk_key):
        """Read in chunk bytes.
        Parameters
        ----------
        chunk_key : str
            Zarr array chunk key.
        Returns
        -------
        bytes
            Bytes of the requested chunk.
        """
        zchunk_key = self._get_chunkstore_key(chunk_key)
        try:
            zchunks = self._ensure_dict(self._store[zchunk_key])
            chunk_loc = zchunks[chunk_key]
        except KeyError:
            raise KeyError(chunk_key)

        # Read chunk's data...
        self._source.seek(chunk_loc['offset'], os.SEEK_SET)
        return self._source.read(chunk_loc['size'])

    def __delitem__(self, chunk_key):
        raise RuntimeError(f'{chunk_key}: Cannot delete chunk')

    def keys(self):
        try:
            for key in self._store.keys():
                if key.endswith(chunks_meta_key):
                    chunks_info = self._ensure_dict(self._store[key])
                    for k in chunks_info.keys():
                        if k == 'source':
                            continue
                        yield k
        except AttributeError:
            raise RuntimeError(
                f'{type(self._store)}: Cannot iterate over store keys')

    def __iter__(self):
        return self.keys()

    def __len__(self):
        """Total number of chunks in the file."""
        total = 0
        try:
            for k in self._store.keys():
                if k.endswith(chunks_meta_key):
                    chunks_info = self._ensure_dict(self._store[k])
                    total += (len(chunks_info) - 1)
        except AttributeError:
            raise RuntimeError(
                f'{type(self._store)}: Does not support counting chunks')
        return total

    def __setitem__(self, chunk_key):
        raise RuntimeError(f'{chunk_key}: Cannot modify chunk data')