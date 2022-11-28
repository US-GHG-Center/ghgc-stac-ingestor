import os

from pypgstac.db import PgstacDB

from .schemas import DashboardCollection
from .utils import get_db_credentials, convert_decimals_to_float, load_into_pgstac, IngestionType
from .vedaloader import VEDALoader


creds = get_db_credentials(os.environ["DB_SECRET_ARN"])

def ingest(collection: DashboardCollection):
    collection = [convert_decimals_to_float(
        collection.dict(by_alias=True, exclude_unset=True)
    )]
    with PgstacDB(dsn=creds.dsn_string, debug=True) as db:
        load_into_pgstac(
            db=db,
            ingestions=collection,
            table=IngestionType.collections
        )

def delete(collection_id: str):
    with PgstacDB(dsn=creds.dsn_string, debug=True) as db:
        loader = VEDALoader(db=db)
        loader.delete_collection(collection_id)
