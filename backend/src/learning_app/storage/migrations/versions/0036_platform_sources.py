"""Add optional platform source identities to the current Windows workspace."""
from alembic import op

revision = "0036_platform_sources"
down_revision = "0035_chat_workspaces"
branch_labels = None
depends_on = None


def upgrade():
    op.execute('CREATE TABLE integration_connections (\n\tconnection_id VARCHAR NOT NULL, \n\tplatform VARCHAR NOT NULL, \n\taccount_id VARCHAR NOT NULL, \n\taccount_label TEXT NOT NULL, \n\tstate VARCHAR NOT NULL, \n\tepoch INTEGER NOT NULL, \n\tPRIMARY KEY (connection_id)\n)')
    op.execute('CREATE TABLE integration_modules (\n\tplatform VARCHAR NOT NULL, \n\tenabled BOOLEAN NOT NULL, \n\tepoch INTEGER NOT NULL, \n\tPRIMARY KEY (platform)\n)')
    op.execute('CREATE TABLE integration_operations (\n\toperation_id VARCHAR NOT NULL, \n\tcourse_id VARCHAR NOT NULL, \n\tconnection_id VARCHAR NOT NULL, \n\tkind VARCHAR NOT NULL, \n\trequest_hash VARCHAR NOT NULL, \n\tstate VARCHAR NOT NULL, \n\treceipt JSON, \n\tPRIMARY KEY (operation_id)\n)')
    op.execute('CREATE TABLE integration_requests (\n\trequest_id VARCHAR NOT NULL, \n\tconnection_id VARCHAR NOT NULL, \n\tpurpose VARCHAR NOT NULL, \n\tstate VARCHAR NOT NULL, \n\tstatus_code INTEGER, \n\tPRIMARY KEY (request_id)\n)')
    op.execute('CREATE TABLE integration_plans (\n\tplan_id VARCHAR NOT NULL, \n\tcourse_id VARCHAR NOT NULL, \n\tconnection_id VARCHAR NOT NULL, \n\tmodule_epoch INTEGER NOT NULL, \n\tconnection_epoch INTEGER NOT NULL, \n\tpayload JSON NOT NULL, \n\tPRIMARY KEY (plan_id), \n\tFOREIGN KEY(course_id) REFERENCES courses (course_id), \n\tFOREIGN KEY(connection_id) REFERENCES integration_connections (connection_id)\n)')
    op.execute('CREATE TABLE integration_sources (\n\tasset_id VARCHAR NOT NULL, \n\tsource_version VARCHAR NOT NULL, \n\tcourse_id VARCHAR NOT NULL, \n\tconnection_id VARCHAR NOT NULL, \n\tobject_id VARCHAR NOT NULL, \n\tpayload JSON NOT NULL, \n\tlocal_path TEXT, \n\tlocal_sha256 VARCHAR, \n\taudio_path TEXT, \n\taudio_sha256 VARCHAR, \n\tPRIMARY KEY (asset_id), \n\tFOREIGN KEY(asset_id) REFERENCES assets (asset_id), \n\tFOREIGN KEY(course_id) REFERENCES courses (course_id), \n\tFOREIGN KEY(connection_id) REFERENCES integration_connections (connection_id)\n)')


def downgrade():
    raise RuntimeError("PLATFORM_DOWNGRADE_DENIED: retain sources and receipts")
