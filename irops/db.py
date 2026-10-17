import os

from falkordb import FalkorDB

_db = None


def graph(name=None):
    global _db
    if _db is None:
        _db = FalkorDB(host=os.getenv("FALKORDB_HOST", "localhost"), port=int(os.getenv("FALKORDB_PORT", 6379)),
                       username=os.getenv("FALKORDB_USERNAME"), password=os.getenv("FALKORDB_PASSWORD"))
    return _db.select_graph(name or os.getenv("IROPS_GRAPH", "irops_WN"))
