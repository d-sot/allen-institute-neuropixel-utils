<strong>Reading HDF5 files with Zarr</strong>

## Installation

Requires latest dev installation of h5py


```bash
$ pip install git+https://github.com/catalystneuro/allen-institute-neuropixel-utils
```


## Usage:

## Reading local data
```python
import zarr
from hdf5zarr import HDF5Zarr, NWBZARRHDF5IO

file_name = '/Users/bendichter/dev/allen-institute-neuropixel-utils/sub-699733573_ses-715093703.nwb'
store = zarr.DirectoryStore('storezarr')
hdf5_zarr = HDF5Zarr(filename = file_name, store=store, store_mode='w', max_chunksize=2*2**20)
zgroup = hdf5_zarr.consolidate_metadata(metadata_key = '.zmetadata')
```
Without indicating a specific zarr store, zarr uses the default `zarr.MemoryStore`.
Alternatively, pass a zarr store such as:
```python
store = zarr.DirectoryStore('storezarr')
hdf5_zarr = HDF5Zarr(file_name, store = store, store_mode = 'w')
```

Examine structure of file using Zarr tools:
```python
# print dataset names
zgroup.tree()
# read
arr = zgroup['units/spike_times']
val = arr[0:1000]
```

Read data into PyNWB:
```python
from hdf5zarr import NWBZARRHDF5IO
io = NWBZARRHDF5IO(mode='r+', file=zgroup)     
```

Export metadata from zarr store to a single json file
```python
import json
metadata_file = 'metadata'
with open(metadata_file, 'w') as f:
    json.dump(zgroup.store.meta_store, f)
```

        
Open NWB file on remote S3 store. requires a loval metadata_file, constructed in previous steps:
```python
import s3fs


fs = s3fs.S3FileSystem(anon=True)

# import metadata from a json file
with open(metadata_file, 'r') as f:
    metadata_dict = json.load(f)

store = metadata_dict
with fs.open('bucketname/' + file_name, 'rb') as f:
    hdf5_zarr = HDF5Zarr(f, store = store, store_mode = 'r')
    zgroup = hdf5_zarr.zgroup
    # print dataset names
    zgroup.tree()
    arr = zgroup['units/spike_times']
    val = arr[0:1000]

```

## Use with nwbwidgets

```python

# In a jupyter notebook:
from nwbwidgets import nwb2widget
f = fs.open('dandiarchive/girder-assetstore/4f/5a/4f5a24f7608041e495c85329dba318b7', 'rb')
hdf5_zarr = HDF5Zarr(f, store = store, store_mode = 'r')
zgroup = hdf5_zarr.zgroup
io = NWBZARRHDF5IO(mode='r', file=zgroup, load_namespaces=True)
nwb = io.read()
nwb2widget(nwb)

```
