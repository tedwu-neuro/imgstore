import os.path
import sqlite3
import logging
import itertools
import operator
import hashlib
import yaml
import numpy as np

from .constants import FRAME_MD


log = logging.getLogger("imgstore.index")


def _load_index(path_without_extension):
    for extension in (".npz", ".yaml"):
        path = path_without_extension + extension
        if os.path.exists(path):
            if extension == ".yaml":
                with open(path, "rt") as f:
                    dat = yaml.safe_load(f)
                    return {k: dat[k] for k in FRAME_MD}
            elif extension == ".npz":
                with open(path, "rb") as f:
                    dat = np.load(f, allow_pickle=True)
                    data = {}
                    for k in FRAME_MD:
                        try:
                            data[k] = dat[k].tolist()
                        except KeyError:
                            log.info(f"{k} is not available in this dataset")

                    return data
        else:
            log.warning(f"{path} is missing")

    raise IOError("could not find index %s" % path_without_extension)


# noinspection SqlNoDataSourceInspection,SqlDialectInspection,SqlResolve
class ImgStoreIndex(object):

    VERSION = "1"

    log = log

    def __init__(self, db=None, path=None, chunk_n_and_chunk_paths=None):
        self._conn = db
        self._path = path
        self._chunk_n_and_chunk_paths = chunk_n_and_chunk_paths


        cur = self._conn.cursor()
        cur.execute("pragma query_only = ON;")

        cur.execute(
            "SELECT value FROM index_information WHERE name = ?", ("version",)
        )
        (v,) = cur.fetchone()
        if v != self.VERSION:
            raise IOError(
                "incorrect index version: %s vs %s" % (v, self.VERSION)
            )

        cur.execute("SELECT COUNT(1) FROM frames")
        (self.frame_count,) = cur.fetchone()

        def _summary(_what):
            cur.execute("SELECT value FROM summary WHERE name = ?", (_what,))
            return cur.fetchone()[0]

        if self.frame_count:
            self.frame_time_max = _summary("frame_time_max")
            self.frame_time_min = _summary("frame_time_min")
            self.frame_max = _summary("frame_max")
            self.frame_min = _summary("frame_min")

            # keep back compat for nan as types (inf -> nan)
            if not np.isreal(self.frame_max):
                self.frame_max = np.nan
            if not np.isreal(self.frame_min):
                self.frame_min = np.nan
        else:
            self.frame_max = self.frame_min = np.nan
            self.frame_time_max = self.frame_time_min = 0.0

        self.log.debug(
            "frame range %f -> %f" % (self.frame_min, self.frame_max)
        )

        # # all chunks in the store [0,1,2, ... ]
        cur.execute("SELECT chunk FROM chunks ORDER BY chunk;")
        self._chunks = tuple(row[0] for row in cur)
        self._chunk_index = None

    @classmethod
    def create_database(cls, conn):
        c = conn.cursor()
        # Create tables
        c.execute(
            "CREATE TABLE frames "
            "(chunk INTEGER, frame_idx INTEGER, frame_number INTEGER, frame_time REAL)"
        )
        c.execute("CREATE TABLE chunks " "(chunk INTEGER, chunk_path TEXT)")
        c.execute("CREATE TABLE index_information " "(name TEXT, value TEXT)")
        c.execute("CREATE TABLE summary " "(name TEXT, value REAL)")
        c.execute(
            "INSERT into index_information VALUES (?, ?)",
            ("version", cls.VERSION),
        )
        c.execute("CREATE INDEX chunk_index ON frames (chunk, frame_idx);")
        conn.commit()

    @classmethod
    def new_from_chunks(cls, chunk_n_and_chunk_paths):
        db = sqlite3.connect(":memory:", check_same_thread=False)
        cls.create_database(db)



        frame_count = 0
        frame_max = -np.inf
        frame_min = np.inf
        frame_time_max = -np.inf
        frame_time_min = np.inf

        cur = db.cursor()

        for chunk_n, chunk_path in sorted(
            chunk_n_and_chunk_paths, key=operator.itemgetter(0)
        ):
            try:
                idx = _load_index(chunk_path)
            except IOError:
                cls.log.warn("missing index for chunk %s" % chunk_n)
                continue

            if not idx["frame_number"]:
                # empty chunk
                continue

            frame_count += len(idx["frame_number"])
            frame_time_min = min(frame_time_min, np.min(idx["frame_time"]))
            frame_time_max = max(frame_time_max, np.max(idx["frame_time"]))
            frame_min = min(frame_min, np.min(idx["frame_number"]))
            frame_max = max(frame_max, np.max(idx["frame_number"]))

            try:
                records = [
                    (chunk_n, i, fn, ft)
                    for i, (fn, ft) in enumerate(
                        zip(idx["frame_number"], idx["frame_time"])
                    )
                ]
            except TypeError:
                cls.log.error("corrupt chunk", exc_info=True)
                continue

            cur.executemany("INSERT INTO frames VALUES (?,?,?,?)", records)
            cur.execute(
                "INSERT INTO chunks VALUES (?, ?)", (chunk_n, chunk_path)
            )

            db.commit()

        cur.execute(
            "INSERT INTO summary VALUES (?,?)",
            ("frame_time_min", float(frame_time_min)),
        )
        cur.execute(
            "INSERT INTO summary VALUES (?,?)",
            ("frame_time_max", float(frame_time_max)),
        )
        cur.execute(
            "INSERT INTO summary VALUES (?,?)", ("frame_min", float(frame_min))
        )
        cur.execute(
            "INSERT INTO summary VALUES (?,?)", ("frame_max", float(frame_max))
        )

        db.commit()

        path = os.path.dirname(chunk_n_and_chunk_paths[0][1])
        return cls(db=db, path=path, chunk_n_and_chunk_paths=chunk_n_and_chunk_paths)

    @classmethod
    def new_from_file(cls, path):
        db = sqlite3.connect(path, check_same_thread=False)
        return cls(db, path=path, chunk_n_and_chunk_paths=None)

    @staticmethod
    def _get_metadata(cur, var_names):
        md = {v: [] for v in var_names}
        for row in cur:
            for i, v in enumerate(var_names):
                md[v].append(row[i])
        return md

    @property
    def chunks(self):
        """the number of non-empty chunks that contain images"""
        return self._chunks

    def to_file(self, path):
        db = sqlite3.connect(path)
        with db:
            for line in self._conn.iterdump():
                # let python handle the transactions
                if line not in ("BEGIN;", "COMMIT;"):
                    db.execute(line)
        db.commit()
        db.close()

    def get_all_metadata(self, rowid=None):
        cur = self._conn.cursor()

        if rowid is not None:

            if rowid > 0:
                pass
            elif rowid < 0:
                order = "DESC"
            else:
                rowid = 1
                log.warning(
                    "rowid=0 is not valid."
                    "Interpreting as rowid=1"
                )

            cmd = "SELECT frame_number, frame_time FROM frames"
            if rowid > 0:
                cmd += f" WHERE rowid={rowid};"
            else:
                cmd += f" ORDER BY rowid {order}"
                cmd += " LIMIT 1;"
        else:
            cmd = "SELECT frame_number, frame_time FROM frames ORDER BY rowid"

        cur.execute(cmd)
        return self._get_metadata(cur, ["frame_number", "frame_time"])

    def get_chunk_metadata(self, chunk_n):
        cur = self._conn.cursor()
        cur.execute(
            "SELECT frame_number, frame_time FROM frames WHERE chunk = ? ORDER BY rowid;",
            (chunk_n,),
        )
        return self._get_metadata(cur, ["frame_number", "frame_time"])

    def get_frame_time(self, frame_number=None, frame_idx=None):
        cur = self._conn.cursor()
        if frame_number is None and not frame_idx is None:
            cur.execute(
                "SELECT frame_time FROM frames WHERE frame_idx = ? ORDER BY rowid;",
                (frame_idx,),
            )
        elif not frame_number is None and frame_idx is None:
            cur.execute(
                "SELECT frame_time FROM frames WHERE frame_number = ? ORDER BY rowid;",
                (frame_number,),
            )
        return self._get_metadata(cur, ["frame_time"])["frame_time"][0]

    def get_chunk_interval(self, chunk_n, metavar="frame_time"):
        """
        Given a chunk number, return the first and last value of the metavar for that chunk
        By default metavar is frame_time, which consists of the time in ms at which each frame was taken
        """
        cur = self._conn.cursor()
        # from https://stackoverflow.com/a/12133952/3541756
        row_number = "row_number() over (order by frame_number desc) as rn"
        row_count = f"count(*) over () as total_count FROM frames WHERE chunk={chunk_n}"
        cur.execute(
            f"SELECT {metavar} from (SELECT {metavar}, {row_number}, {row_count}) where rn=1 or rn=total_count ORDER BY rn DESC;"
        )

        data = []
        for d in cur:
            data.append(d[0])
        if data:
            start, end = data
            return start, end
        else:
            return None

    @property
    def chunk_index(self):

        cache_dir = os.path.join(self._path, "cache")
        os.makedirs(cache_dir, exist_ok=True)
        data = sorted(self._chunk_n_and_chunk_paths, key=lambda x: x[0])
        hashable = "".join([str(e) for e in list(itertools.chain(*data))])
        hash_res = hashlib.md5(hashable.encode()).hexdigest()
        cached_index = os.path.join(cache_dir, f"chunk_index_{hash_res}.npy")
        print(cached_index)

        if self._chunk_index is None:

            if os.path.exists(cached_index):
                self._chunk_index = np.load(cached_index, allow_pickle=True).item()
            else:
                self._chunk_index = {
                    "frame_number": {chunk: self.get_chunk_interval(chunk, "frame_number") for chunk in self._chunks},
                    "frame_time": {chunk: self.get_chunk_interval(chunk, "frame_time") for chunk in self._chunks}
                }
                np.save(cached_index, self._chunk_index)

        return self._chunk_index

    def get_chunk_and_frame_idx(self, frame_number):
        logging.warning("Deprecated. Use get_chunk_and_frame_idx_from_frame_number")
        return self.get_chunk_and_frame_idx_from_frame_number(frame_number)

    def get_chunk_and_frame_idx_from_frame_time(self, frame_time):
        return self.get_chunk_and_frame_idx_(frame_time, "frame_time")

    def get_chunk_and_frame_idx_from_frame_number(self, frame_number):
        return self.get_chunk_and_frame_idx_(frame_number, "frame_number")


    def get_chunk_and_frame_idx_(self, value, metavar="frame_time"):
        """
        Given a frame_number, return the chunk to which it belongs,
        and the index of the frame inside the chunk
        (where the first frame of the chunk has index 0)
        """
        cur = self._conn.cursor()
        cur.execute(
            f"SELECT chunk, frame_idx FROM frames where {metavar} = {value}"
        )
        data = []
        for d in cur:
            data.extend(d)
        if data:
            chunk, frame_idx = data
            return chunk, frame_idx
        else:
            return None


    def find_chunk(self, what, value):
        assert what in ("frame_number", "frame_time", "index")
        cur = self._conn.cursor()

        if what == "index":
            cur.execute(
                "SELECT chunk, frame_idx FROM frames ORDER BY rowid LIMIT 1 OFFSET {};".format(
                    int(value)
                )
            )
        else:
            cur.execute(
                "SELECT chunk, frame_idx FROM frames WHERE {} = ?;".format(
                    what
                ),
                (value,),
            )

        try:
            chunk_n, frame_idx = cur.fetchone()
        except TypeError:  # no result
            return -1, -1

        return chunk_n, frame_idx

    def find_chunk_nearest(self, query, value, target = "chunk, frame_idx", direction="all"):
        assert query in ("frame_number", "frame_time")
        cur = self._conn.cursor()
        
        if direction=="all":
            cmd = (
                f"SELECT {target}"
                " FROM frames ORDER BY ABS(? - {}) LIMIT 1;".format(
                    query
                ),
                (value,)
            )
        
        elif direction=="future":
            cmd = (
                f"SELECT {target}"
                " FROM frames"
                " WHERE (? - {}) <= 0"
                " ORDER BY ABS(? - {})"
                " LIMIT 1;".format(query, query),
                (value, value,)

            )
        
        elif direction=="past":
            cmd = (
                f"SELECT {target}"
                " FROM frames"
                " WHERE (? - {}) >= 0"
                " ORDER BY ABS(? - {})"
                " LIMIT 1;".format(query, query),
                (value, value,)

            )

        cur.execute(
            *cmd,
        )

        data = cur.fetchone()

        if data is None:
            return self.find_chunk_nearest(query, value, direction="all")
        else:
            chunk_n, frame_idx = data

        return chunk_n, frame_idx
