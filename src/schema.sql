-- DuckDB schema for the reference rates loaded at app startup.
-- Hospital prices are NOT here: they are the partitioned Parquet dataset under
-- data/hospital_rates/ (schema documented at the bottom), queried via parquet glob.
-- Procedure descriptions/categories come from config/allowlist.yaml; hospital names
-- from config/hospitals.yaml — neither needs a DB table.

-- Medicare benchmark rates (the negotiation floor): PFS | OPPS | IPPS | CLFS.
CREATE TABLE IF NOT EXISTS medicare_rates (
    code       VARCHAR NOT NULL,
    code_type  VARCHAR NOT NULL,     -- CPT | HCPCS | DRG
    locality   VARCHAR,
    rate       DOUBLE,
    year       INTEGER NOT NULL,
    source     VARCHAR NOT NULL,     -- PFS | OPPS | IPPS | CLFS
    PRIMARY KEY (code, code_type, locality, year, source)
);

-- hospital_rates Parquet schema (partitioned by state, then code):
--   hospital_id, state, code, code_type,
--   cash_price, negotiated_min, negotiated_median, negotiated_max
