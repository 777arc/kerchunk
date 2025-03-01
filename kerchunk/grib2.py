import base64
import copy
import io
import logging
from collections import defaultdict
import warnings
from typing import Iterable, List, Dict, Set, TYPE_CHECKING, Optional, Callable
import ujson
import itertools

if TYPE_CHECKING:
    import pandas as pd
    import datatree

import fsspec
import zarr
import xarray
import numpy as np

from kerchunk.utils import class_factory, _encode_for_JSON
from kerchunk.codecs import GRIBCodec
from kerchunk.combine import MultiZarrToZarr, drop


try:
    import cfgrib
except ModuleNotFoundError as err:  # pragma: no cover
    if err.name == "cfgrib":
        raise ImportError(
            "cfgrib is needed to kerchunk GRIB2 files. Please install it with "
            "`conda install -c conda-forge cfgrib`. See https://github.com/ecmwf/cfgrib "
            "for more details."
        )


class DynamicZarrStoreError(ValueError):
    pass


# cfgrib copies over certain GRIB attributes
# but renames them to CF-compliant values
ATTRS_TO_COPY_OVER = {
    "long_name": "GRIB_name",
    "units": "GRIB_units",
    "standard_name": "GRIB_cfName",
}

logger = logging.getLogger("grib2-to-zarr")


def _split_file(f: io.FileIO, skip=0):
    if hasattr(f, "size"):
        size = f.size
    else:
        size = f.seek(0, 2)
        f.seek(0)
    part = 0

    while f.tell() < size:
        logger.debug(f"extract part {part + 1}")
        head = f.read(1024)
        if b"GRIB" not in head:
            f.seek(-4, 1)
            continue
        ind = head.index(b"GRIB")
        start = f.tell() - len(head) + ind
        part_size = int.from_bytes(head[ind + 12 : ind + 16], "big")
        f.seek(start)
        yield start, part_size, f.read(part_size)
        part += 1
        if skip and part >= skip:
            break


def _store_array(store, z, data, var, inline_threshold, offset, size, attr):
    nbytes = data.dtype.itemsize
    for i in data.shape:
        nbytes *= i

    shape = tuple(data.shape or ())
    if nbytes < inline_threshold:
        logger.debug(f"Store {var} inline")
        d = z.create_dataset(
            name=var,
            shape=shape,
            chunks=shape,
            dtype=data.dtype,
            fill_value=attr.get("missingValue", None),
            compressor=False,
        )
        if hasattr(data, "tobytes"):
            b = data.tobytes()
        else:
            b = data.build_array().tobytes()
        try:
            # easiest way to test if data is ascii
            b.decode("ascii")
        except UnicodeDecodeError:
            b = b"base64:" + base64.b64encode(data)
        store[f"{var}/0"] = b.decode("ascii")
    else:
        logger.debug(f"Store {var} reference")
        d = z.create_dataset(
            name=var,
            shape=shape,
            chunks=shape,
            dtype=data.dtype,
            fill_value=attr.get("missingValue", None),
            filters=[GRIBCodec(var=var, dtype=str(data.dtype))],
            compressor=False,
            overwrite=True,
        )
        store[f"{var}/" + ".".join(["0"] * len(shape))] = ["{{u}}", offset, size]
    d.attrs.update(attr)


def scan_grib(
    url,
    common=None,
    storage_options=None,
    inline_threshold=100,
    skip=0,
    filter={},
) -> List[Dict]:
    """
    Generate references for a GRIB2 file

    Parameters
    ----------

    url: str
        File location
    common_vars: (depr, do not use)
    storage_options: dict
        For accessing the data, passed to filesystem
    inline_threshold: int
        If given, store array data smaller than this value directly in the output
    skip: int
        If non-zero, stop processing the file after this many messages
    filter: dict
        keyword filtering. For each key, only messages where the key exists and has
        the exact value or is in the given set, are processed.
        E.g., the cf-style filter ``{'typeOfLevel': 'heightAboveGround', 'level': 2}``
        only keeps messages where heightAboveGround==2.

    Returns
    -------

    list(dict): references dicts in Version 1 format, one per message in the file
    """
    import eccodes

    storage_options = storage_options or {}
    logger.debug(f"Open {url}")

    # This is hardcoded a lot in cfgrib!
    # valid_time is added if "time" and "step" are present in time_dims
    # These are present by default
    # TIME_DIMS = ["step", "time", "valid_time"]

    out = []
    with fsspec.open(url, "rb", **storage_options) as f:
        logger.debug(f"File {url}")
        for offset, size, data in _split_file(f, skip=skip):
            store = {}
            mid = eccodes.codes_new_from_message(data)
            m = cfgrib.cfmessage.CfMessage(mid)

            # It would be nice to just have a list of valid keys
            # There does not seem to be a nice API for this
            # 1. message_grib_keys returns keys coded in the message
            # 2. There exist "computed" keys, that are functions applied on the data
            # 3. There are also aliases!
            #    e.g. "number" is an alias of "perturbationNumber", and cfgrib uses this alias
            # So we stick to checking membership in 'm', which ends up doing
            # a lot of reads.
            message_keys = set(m.message_grib_keys())
            # The choices here copy cfgrib :(
            # message_keys.update(cfgrib.dataset.INDEX_KEYS)
            # message_keys.update(TIME_DIMS)
            # print("totalNumber" in cfgrib.dataset.INDEX_KEYS)
            # Adding computed keys adds a lot that isn't added by cfgrib
            # message_keys.extend(m.computed_keys)

            shape = (m["Ny"], m["Nx"])
            # thank you, gribscan
            native_type = eccodes.codes_get_native_type(m.codes_id, "values")
            data_size = eccodes.codes_get_size(m.codes_id, "values")
            coordinates = []

            good = True
            for k, v in (filter or {}).items():
                if k not in m:
                    good = False
                elif isinstance(v, (list, tuple, set)):
                    if m[k] not in v:
                        good = False
                elif m[k] != v:
                    good = False
            if good is False:
                continue

            z = zarr.open_group(store)
            global_attrs = {
                f"GRIB_{k}": m[k]
                for k in cfgrib.dataset.GLOBAL_ATTRIBUTES_KEYS
                if k in m
            }
            if "GRIB_centreDescription" in global_attrs:
                # follow CF compliant renaming from cfgrib
                global_attrs["institution"] = global_attrs["GRIB_centreDescription"]
            z.attrs.update(global_attrs)

            if data_size < inline_threshold:
                # read the data
                vals = m["values"].reshape(shape)
            else:
                # dummy array to match the required interface
                vals = np.empty(shape, dtype=native_type)
                assert vals.size == data_size

            attrs = {
                # Follow cfgrib convention and rename key
                f"GRIB_{k}": m[k]
                for k in cfgrib.dataset.DATA_ATTRIBUTES_KEYS
                + cfgrib.dataset.EXTRA_DATA_ATTRIBUTES_KEYS
                + cfgrib.dataset.GRID_TYPE_MAP.get(m["gridType"], [])
                if k in m
            }
            for k, v in ATTRS_TO_COPY_OVER.items():
                if v in attrs:
                    attrs[k] = attrs[v]

            # try to use cfVarName if available,
            # otherwise use the grib shortName
            varName = m["cfVarName"]
            if varName in ("undef", "unknown"):
                varName = m["shortName"]
            _store_array(store, z, vals, varName, inline_threshold, offset, size, attrs)
            if "typeOfLevel" in message_keys and "level" in message_keys:
                name = m["typeOfLevel"]
                coordinates.append(name)
                # convert to numpy scalar, so that .tobytes can be used for inlining
                # dtype=float is hardcoded in cfgrib
                data = np.array(m["level"], dtype=float)[()]
                try:
                    attrs = cfgrib.dataset.COORD_ATTRS[name]
                except KeyError:
                    logger.debug(f"Couldn't find coord {name} in dataset")
                    attrs = {}
                attrs["_ARRAY_DIMENSIONS"] = []
                _store_array(
                    store, z, data, name, inline_threshold, offset, size, attrs
                )
            dims = (
                ["y", "x"]
                if m["gridType"] in cfgrib.dataset.GRID_TYPES_2D_NON_DIMENSION_COORDS
                else ["latitude", "longitude"]
            )
            z[varName].attrs["_ARRAY_DIMENSIONS"] = dims

            for coord in cfgrib.dataset.COORD_ATTRS:
                coord2 = {
                    "latitude": "latitudes",
                    "longitude": "longitudes",
                    "step": "step:int",
                }.get(coord, coord)
                try:
                    x = m.get(coord2)
                except eccodes.WrongStepUnitError as e:
                    logger.warning(
                        "Ignoring coordinate '%s' for varname '%s', raises: eccodes.WrongStepUnitError(%s)",
                        coord2,
                        varName,
                        e,
                    )
                    continue

                if x is None:
                    continue
                coordinates.append(coord)
                inline_extra = 0
                if isinstance(x, np.ndarray) and x.size == data_size:
                    if (
                        m["gridType"]
                        in cfgrib.dataset.GRID_TYPES_2D_NON_DIMENSION_COORDS
                    ):
                        dims = ["y", "x"]
                        x = x.reshape(vals.shape)
                    else:
                        dims = [coord]
                        if coord == "latitude":
                            x = x.reshape(vals.shape)[:, 0].copy()
                        elif coord == "longitude":
                            x = x.reshape(vals.shape)[0].copy()
                        # force inlining of x/y/latitude/longitude coordinates.
                        # since these are derived from analytic formulae
                        # and are not stored in the message
                        inline_extra = x.nbytes + 1
                elif np.isscalar(x):
                    # convert python scalars to numpy scalar
                    # so that .tobytes can be used for inlining
                    x = np.array(x)[()]
                    dims = []
                else:
                    x = np.array([x])
                    dims = [coord]
                attrs = cfgrib.dataset.COORD_ATTRS[coord]
                _store_array(
                    store,
                    z,
                    x,
                    coord,
                    inline_threshold + inline_extra,
                    offset,
                    size,
                    attrs,
                )
                z[coord].attrs["_ARRAY_DIMENSIONS"] = dims
            if coordinates:
                z.attrs["coordinates"] = " ".join(coordinates)

            out.append(
                {
                    "version": 1,
                    "refs": _encode_for_JSON(store),
                    "templates": {"u": url},
                }
            )
    logger.debug("Done")
    return out


GribToZarr = class_factory(scan_grib)


def example_combine(
    filter={"typeOfLevel": "heightAboveGround", "level": 2}
):  # pragma: no cover
    """Create combined dataset of weather measurements at 2m height

    Ten consecutive timepoints from ten 120MB files on s3.
    Example usage:

    >>> tot = example_combine()
    >>> ds = xr.open_dataset("reference://", engine="zarr", backend_kwargs={
    ...        "consolidated": False,
    ...        "storage_options": {"fo": tot, "remote_options": {"anon": True}}})
    """
    files = [
        "s3://noaa-hrrr-bdp-pds/hrrr.20190101/conus/hrrr.t22z.wrfsfcf01.grib2",
        "s3://noaa-hrrr-bdp-pds/hrrr.20190101/conus/hrrr.t23z.wrfsfcf01.grib2",
        "s3://noaa-hrrr-bdp-pds/hrrr.20190102/conus/hrrr.t00z.wrfsfcf01.grib2",
        "s3://noaa-hrrr-bdp-pds/hrrr.20190102/conus/hrrr.t01z.wrfsfcf01.grib2",
        "s3://noaa-hrrr-bdp-pds/hrrr.20190102/conus/hrrr.t02z.wrfsfcf01.grib2",
        "s3://noaa-hrrr-bdp-pds/hrrr.20190102/conus/hrrr.t03z.wrfsfcf01.grib2",
        "s3://noaa-hrrr-bdp-pds/hrrr.20190102/conus/hrrr.t04z.wrfsfcf01.grib2",
        "s3://noaa-hrrr-bdp-pds/hrrr.20190102/conus/hrrr.t05z.wrfsfcf01.grib2",
        "s3://noaa-hrrr-bdp-pds/hrrr.20190102/conus/hrrr.t06z.wrfsfcf01.grib2",
    ]
    so = {"anon": True, "default_cache_type": "readahead"}

    out = [scan_grib(u, storage_options=so, filter=filter) for u in files]
    out = sum(out, [])
    mzz = MultiZarrToZarr(
        out,
        remote_protocol="s3",
        preprocess=drop(("valid_time", "step")),
        remote_options=so,
        concat_dims=["time", "var"],
        identical_dims=["heightAboveGround", "latitude", "longitude"],
    )
    return mzz.translate()


def grib_tree(
    message_groups: Iterable[Dict],
    remote_options=None,
) -> Dict:
    """
    Build a hierarchical data model from a set of scanned grib messages.

    The iterable input groups should be a collection of results from scan_grib. Multiple grib files can
    be processed together to produce an FMRC like collection.
    The time (reference_time) and step coordinates will be used as concat_dims in the MultiZarrToZarr
    aggregation. Each variable name will become a group with nested subgroups representing the grib
    step type and grib level. The resulting hierarchy can be opened as a zarr_group or a xarray datatree.
    Grib message variable names that decode as "unknown" are dropped
    Grib typeOfLevel attributes that decode as unknown are treated as a single group
    Grib steps that are missing due to WrongStepUnitError are patched with NaT
    The input message_groups should not be modified by this method

    Parameters
    ----------
    message_groups: iterable[dict]
        a collection of zarr store like dictionaries as produced by scan_grib
    remote_options: dict
        remote options to pass to MultiZarrToZarr

    Returns
    -------
    dict: A new zarr store like dictionary for use as a reference filesystem mapper with zarr
    or xarray datatree
    """
    # Hard code the filters in the correct order for the group hierarchy
    filters = ["stepType", "typeOfLevel"]

    # TODO allow passing a LazyReferenceMapper as output?
    zarr_store = {}
    zroot = zarr.open_group(store=zarr_store)

    aggregations: Dict[str, List] = defaultdict(list)
    aggregation_dims: Dict[str, Set] = defaultdict(set)

    unknown_counter = 0
    for msg_ind, group in enumerate(message_groups):
        assert group["version"] == 1

        gattrs = ujson.loads(group["refs"][".zattrs"])
        coordinates = gattrs["coordinates"].split(" ")

        # Find the data variable
        vname = None
        for key, entry in group["refs"].items():
            name = key.split("/")[0]
            if name not in [".zattrs", ".zgroup"] and name not in coordinates:
                vname = name
                break

        if vname is None:
            raise RuntimeError(
                f"Can not find a data var for msg# {msg_ind} in {group['refs'].keys()}"
            )

        if vname == "unknown":
            # To resolve unknown variables add custom grib tables.
            # https://confluence.ecmwf.int/display/UDOC/Creating+your+own+local+definitions+-+ecCodes+GRIB+FAQ
            # If you process the groups from a single file in order, you can use the msg# to compare with the
            # IDX file. The idx files message index is 1 based where the grib_tree message count is zero based
            logger.warning(
                "Dropping unknown variable in msg# %d. Compare with the grib idx file to help identify it"
                " and build an ecCodes local grib definitions file to fix it.",
                msg_ind,
            )
            unknown_counter += 1
            continue

        logger.debug("Processing vname: %s", vname)
        dattrs = ujson.loads(group["refs"][f"{vname}/.zattrs"])
        # filter order matters - it determines the hierarchy
        gfilters = {}
        for key in filters:
            attr_val = dattrs.get(f"GRIB_{key}")
            if attr_val is None:
                continue
            if attr_val == "unknown":
                logger.warning(
                    "Found 'unknown' attribute value for key %s in var %s of msg# %s",
                    key,
                    vname,
                    msg_ind,
                )
                # Use unknown as a group or drop it?

            gfilters[key] = attr_val

        zgroup = zroot.require_group(vname)
        if "name" not in zgroup.attrs:
            zgroup.attrs["name"] = dattrs.get("GRIB_name")

        for key, value in gfilters.items():
            if value:  # Ignore empty string and None
                # name the group after the attribute values: surface, instant, etc
                zgroup = zgroup.require_group(value)
                # Add an attribute to give context
                zgroup.attrs[key] = value

        # Set the coordinates attribute for the group
        zgroup.attrs["coordinates"] = " ".join(coordinates)
        # add to the list of groups to multi-zarr
        aggregations[zgroup.path].append(group)

        # keep track of the level coordinate variables and their values
        for key, entry in group["refs"].items():
            name = key.split("/")[0]
            if name == gfilters.get("typeOfLevel") and key.endswith("0"):
                if isinstance(entry, list):
                    entry = tuple(entry)
                aggregation_dims[zgroup.path].add(entry)

    concat_dims = ["time", "step"]
    identical_dims = ["longitude", "latitude"]
    for path in aggregations.keys():
        # Parallelize this step!
        catdims = concat_dims.copy()
        idims = identical_dims.copy()

        level_dimension_value_count = len(aggregation_dims.get(path, ()))
        level_group_name = path.split("/")[-1]
        if level_dimension_value_count == 0:
            logger.debug(
                "Path % has no value coordinate value associated with the level name %s",
                path,
                level_group_name,
            )
        elif level_dimension_value_count == 1:
            idims.append(level_group_name)
        elif level_dimension_value_count > 1:
            # The level name should be the last element in the path
            catdims.insert(3, level_group_name)

        logger.info(
            "%s calling MultiZarrToZarr with idims %s and catdims %s",
            path,
            idims,
            catdims,
        )

        mzz = MultiZarrToZarr(
            aggregations[path],
            remote_options=remote_options,
            concat_dims=catdims,
            identical_dims=idims,
        )
        group = mzz.translate()

        for key, value in group["refs"].items():
            if key not in [".zattrs", ".zgroup"]:
                zarr_store[f"{path}/{key}"] = value

    # Force all stored values to decode as string, not bytes. String should be correct.
    # ujson will reject bytes values by default.
    # Using 'reject_bytes=False' one write would fail an equality check on read.
    zarr_store = {
        key: (val.decode() if isinstance(val, bytes) else val)
        for key, val in zarr_store.items()
    }
    # TODO handle other kerchunk reference spec versions?
    result = dict(refs=zarr_store, version=1)

    return result


def correct_hrrr_subhf_step(group: Dict) -> Dict:
    """
    Overrides the definition of the "step" variable.

    Sets the value equal to the `valid_time - time`
    in hours as a floating point value. This fixes issues with the HRRR SubHF grib2 step as read by
    cfgrib via scan_grib.
    The result is a deep copy, the original data is unmodified.

    Parameters
    ----------
    group: dict
        the zarr group store for a single grib message

    Returns
    -------
    dict: A new zarr store like dictionary for use as a reference filesystem mapper with zarr
    or xarray datatree
    """
    group = copy.deepcopy(group)
    group["refs"]["step/.zarray"] = (
        '{"chunks":[],"compressor":null,"dtype":"<f8","fill_value":"NaN","filters":null,"order":"C",'
        '"shape":[],"zarr_format":2}'
    )
    group["refs"]["step/.zattrs"] = (
        '{"_ARRAY_DIMENSIONS":[],"long_name":"time since forecast_reference_time",'
        '"standard_name":"forecast_period","units":"hours"}'
    )

    # add step to coords
    attrs = ujson.loads(group["refs"][".zattrs"])
    if "step" not in attrs["coordinates"]:
        attrs["coordinates"] += " step"
    group["refs"][".zattrs"] = ujson.dumps(attrs)

    fo = fsspec.filesystem("reference", fo=group, mode="r")
    xd = xarray.open_dataset(fo.get_mapper(), engine="zarr", consolidated=False)

    correct_step = xd.valid_time.values - xd.time.values

    assert correct_step.shape == ()
    step_float = correct_step.astype("timedelta64[s]").astype("float") / 3600.0
    step_bytes = step_float.tobytes()
    try:
        enocded_val = step_bytes.decode("ascii")
    except UnicodeDecodeError:
        enocded_val = (b"base64:" + base64.b64encode(step_bytes)).decode("ascii")

    group["refs"]["step/0"] = enocded_val

    return group


def parse_grib_idx(
    basename: str,
    suffix: str = "idx",
    storage_options: Optional[Dict] = None,
    validate: bool = False,
) -> "pd.DataFrame":
    """
    Parses per-message metadata from a grib2.idx file (text-type) to a dataframe of attributes

    The function uses the idx file, extracts the metadata known as attrs (variables with
    level and forecast time) from each idx entry and converts it into pandas
    DataFrame. The dataframe is later to build the one-to-one mapping to the grib file metadata.

    Parameters
    ----------
    basename : str
        The base name is the full path to the grib file.
    suffix : str
        The suffix is the ending for the idx file.
    storage_options: dict
        For accessing the data, passed to filesystem
    validate : bool
        The validation if the metadata table has duplicate attrs.

    Returns
    -------
    pandas.DataFrame : The data frame containing the results.
    """
    import pandas as pd

    fs, _ = fsspec.core.url_to_fs(basename, **(storage_options or {}))

    fname = f"{basename}.{suffix}"

    baseinfo = fs.info(basename)

    with fs.open(fname) as f:
        result = pd.read_csv(f, header=None, names=["raw_data"])
        result[["idx", "offset", "date", "attrs"]] = result["raw_data"].str.split(
            ":", expand=True, n=3
        )
        result["offset"] = result["offset"].astype(int)
        result["idx"] = result["idx"].astype(int)

        # dropping the original single "raw_data" column after formatting
        result.drop(columns=["raw_data"], inplace=True)

    result = result.assign(
        length=(
            result.offset.shift(periods=-1, fill_value=baseinfo["size"]) - result.offset
        ),
        idx_uri=fname,
        grib_uri=basename,
    )

    if validate and not result["attrs"].is_unique:
        raise ValueError(f"Attribute mapping for grib file {basename} is not unique")

    return result.set_index("idx")


def build_path(path: Iterable[str | None], suffix: Optional[str] = None) -> str:
    """
    Returns the path to access the values in a zarr store without a leading "/"

    Parameters
    ----------
    path : Iterable[str | None]
        The path is the list of values to the element in zarr store
    suffix : str
        Last element if any

    Returns
    -------
    str : returns the path as a string
    """
    return "/".join([val for val in [*path, suffix] if val is not None]).lstrip("/")


def extract_dataset_chunk_index(
    dset: "datatree.DataTree",
    ref_store: Dict,
    grib: bool = False,
) -> list[dict]:
    """
    Process and extract a kerchunk index for an xarray dataset or datatree node.

    The data_vars from the dataset will be indexed.
    The coordinate vars for each dataset will be used for indexing.
    Datatrees generated by grib_tree have some nice properties which allow a denser index.

    Parameters
    ----------
    dset : datatree.DataTree
        The datatree node from the datatree instance
    ref_store : Dict
        The zarr store dictionary backed by the gribtree
    grib : bool
        boolean for treating coordinates as grib levels

    Returns
    -------
    list[dict] : returns the extracted grib metadata in the form of key-value pairs inside a list
    """
    import datatree

    result: list[dict] = []
    attributes = dset.attrs.copy()

    dpath = None
    if isinstance(dset, datatree.DataTree):
        dpath = dset.path
        walk_group = dset.parent
        while walk_group:
            attributes.update(walk_group.attrs)
            walk_group = walk_group.parent

    for dname, dvar in dset.data_vars.items():
        # Get the chunk size - `chunks` property only works for xarray native
        zarray = ujson.loads(ref_store[build_path([dpath, dname], suffix=".zarray")])
        dchunk = zarray["chunks"]
        dshape = dvar.shape

        index_dims = {}
        for ddim_nane, ddim_size, dchunk_size in zip(dvar.dims, dshape, dchunk):
            if dchunk_size == 1:
                index_dims[ddim_nane] = ddim_size
            elif dchunk_size != ddim_size:
                # Must be able to get a single coordinate value for each chunk to index it.
                raise ValueError(
                    "Can not extract chunk index for dimension %s with non singleton chunk dimensions"
                    % ddim_nane
                )
            # Drop the dim where each chunk covers the whole dimension - no indexing needed!

        for idx in itertools.product(*[range(v) for v in index_dims.values()]):
            # Build an iterator over each of the single dimension chunks
            dim_idx = {key: val for key, val in zip(index_dims.keys(), idx)}

            coord_vals = {}
            for cname, cvar in dvar.coords.items():
                if grib:
                    # Grib data has only one level coordinate
                    cname = (
                        cname
                        if cname
                        in ("valid_time", "time", "step", "latitude", "longitude")
                        else "level"
                    )

                if all([dim_name in dim_idx for dim_name in cvar.dims]):
                    coord_index = tuple([dim_idx[dim_name] for dim_name in cvar.dims])
                    try:
                        coord_vals[cname] = cvar.to_numpy()[coord_index]
                    except Exception:
                        raise DynamicZarrStoreError(
                            f"Error reading coords for {dpath}/{dname} coord {cname} with index {coord_index}"
                        )

            whole_dim_cnt = len(dvar.dims) - len(dim_idx)
            chunk_idx = map(str, [*idx, *[0] * whole_dim_cnt])
            chunk_key = build_path([dpath, dname], suffix=".".join(chunk_idx))
            chunk_ref = ref_store.get(chunk_key)

            if chunk_ref is None:
                logger.warning("Chunk not found: %s", chunk_key)
                continue

            elif isinstance(chunk_ref, list) and len(chunk_ref) == 3:
                chunk_data = dict(
                    uri=chunk_ref[0],
                    offset=chunk_ref[1],
                    length=chunk_ref[2],
                    inline_value=None,
                )
            elif isinstance(chunk_ref, (bytes, str)):
                chunk_data = dict(inline_value=chunk_ref, offset=-1, length=-1)
            else:
                raise ValueError(f"Key {chunk_key} has bad value '{chunk_ref}'")
            result.append(dict(varname=dname, **attributes, **coord_vals, **chunk_data))

    return result


def extract_datatree_chunk_index(
    dtree: "datatree.DataTree", kerchunk_store: dict, grib: bool = False
) -> "pd.DataFrame":
    """
    Recursive method to iterate over the data tree and extract the data variable chunks with index metadata

    Parameters
    ----------
    dtree : datatree.DataTree
        The xarray datatree representation of the reference filesystem
    kerchunk_store : dict
        the grib_tree output for a single grib file
    grib : bool
        boolean for treating coordinates as grib levels

    Returns
    -------
    pandas.Dataframe : The dataframe constructed from the grib metadata
    """
    import pandas as pd

    result: list[dict] = []

    for node in dtree.subtree:
        if node.has_data:
            result += extract_dataset_chunk_index(
                node, kerchunk_store["refs"], grib=grib
            )

    return pd.DataFrame.from_records(result)


def _map_grib_file_by_group(
    fname: str,
    mapper: Optional[Callable] = None,
    storage_options: Optional[Dict] = None,
) -> "pd.DataFrame":
    """
    Helper method used to read the cfgrib metadata associated with each message (group) in the grib file
    This method does not add metadata

    Parameters
    ----------
    fname : str
        the file name to read with scan_grib
    mapper : Optional[Callable]
        the mapper if any to apply (used for hrrr subhf)

    Returns
    -------
    pandas.Dataframe : The intermediate dataframe constructed from the grib metadata
    """
    import pandas as pd

    mapper = (lambda x: x) if mapper is None else mapper
    references = scan_grib(fname, storage_options=storage_options)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return pd.concat(
            # grib idx is fortran indexed (from one not zero)
            list(
                filter(
                    # filtering out the cfgrib metadata dataframe in case it is None
                    lambda item: item is not None,
                    [
                        # extracting the metadata from a single message
                        _extract_single_group(mapper(group), i, storage_options)
                        for i, group in enumerate(references, start=1)
                    ],
                )
            )
        ).set_index("idx")


def _extract_single_group(grib_group: dict, idx: int, storage_options: Dict):
    import datatree

    grib_tree_store = grib_tree(
        [
            grib_group,
        ],
        storage_options,
    )

    if len(grib_tree_store["refs"]) <= 1:
        logger.info("Empty DT: %s", grib_tree_store)
        return None

    dt = datatree.open_datatree(
        fsspec.filesystem("reference", fo=grib_tree_store).get_mapper(""),
        engine="zarr",
        consolidated=False,
    )

    k_ind = extract_datatree_chunk_index(dt, grib_tree_store, grib=True)
    if k_ind.empty:
        logger.warning("Empty Kind: %s", grib_tree_store)
        return None

    assert (
        len(k_ind) == 1
    ), f"expected a single variable grib group but produced: {k_ind}"
    k_ind.loc[:, "idx"] = idx
    return k_ind


def build_idx_grib_mapping(
    basename: str,
    storage_options: Optional[Dict] = None,
    suffix: str = "idx",
    mapper: Optional[Callable] = None,
    validate: bool = True,
) -> "pd.DataFrame":
    """
    Mapping method combines the idx and grib metadata to make a mapping from
    one to the other for a particular model horizon file. This should be generally
    applicable to all forecasts for the given horizon.

    Parameters
    ----------
    basename : str
        the full path for the grib2 file
    storage_options: dict
        For accessing the data, passed to filesystem
    suffix : str
        The suffix is the ending for the idx file.
    mapper : Optional[Callable]
        the mapper if any to apply (used for hrrr subhf)
    validate : bool
        to assert the mapping is correct or fail before returning

    Returns
    -------
    pandas.Dataframe : The merged dataframe with the results of the two operations
    joined on the grib message (group) number
    """
    import pandas as pd

    grib_file_index = _map_grib_file_by_group(
        fname=basename,
        mapper=mapper,
        storage_options=storage_options,
    )
    idx_file_index = parse_grib_idx(
        basename=basename, suffix=suffix, storage_options=storage_options
    )
    result = idx_file_index.merge(
        # Left merge because the idx file should be authoritative - one record per grib message
        grib_file_index,
        on="idx",
        how="left",
        suffixes=("_idx", "_grib"),
    )

    if validate:
        # If any of these conditions fail - inspect the result manually on colab.
        all_match_offset = (
            (result.loc[:, "offset_idx"] == result.loc[:, "offset_grib"])
            | pd.isna(result.loc[:, "offset_grib"])
            | ~pd.isna(result.loc[:, "inline_value"])
        )
        all_match_length = (
            (result.loc[:, "length_idx"] == result.loc[:, "length_grib"])
            | pd.isna(result.loc[:, "length_grib"])
            | ~pd.isna(result.loc[:, "inline_value"])
        )

        if not all_match_offset.all():
            vcs = all_match_offset.value_counts()
            raise ValueError(
                f"Failed to match message offset mapping for grib file {basename}: "
                f"{vcs[True]} matched, {vcs[False]} didn't"
            )

        if not all_match_length.all():
            vcs = all_match_length.value_counts()
            raise ValueError(
                f"Failed to match message offset mapping for grib file {basename}: "
                f"{vcs[True]} matched, {vcs[False]} didn't"
            )

        if not result["attrs"].is_unique:
            dups = result.loc[result["attrs"].duplicated(keep=False), :]
            logger.warning(
                "The idx attribute mapping for %s is not unique for %d variables: %s",
                basename,
                len(dups),
                dups.varname.tolist(),
            )

        r_index = result.set_index(
            ["varname", "typeOfLevel", "stepType", "level", "valid_time"]
        )
        if not r_index.index.is_unique:
            dups = r_index.loc[r_index.index.duplicated(keep=False), :]
            logger.warning(
                "The grib hierarchy in %s is not unique for %d variables: %s",
                basename,
                len(dups),
                dups.index.get_level_values("varname").tolist(),
            )

    return result
