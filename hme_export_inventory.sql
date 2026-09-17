-- Export the HME tenant allowlist for the (separate) extractor application.
-- The extractor fails closed if a querydata result contains a store number that
-- is not in this list, which is what prevents a lost scope filter from silently
-- ingesting another HME customer's stores.
\copy (SELECT json_agg(json_build_object('hme_store_number', hme_store_number, 'hme_store_name', hme_store_name) ORDER BY hme_store_number) FROM hme_store_mapping WHERE active) TO PROGRAM 'cat > /var/lib/laynes/hme/state/tenant_inventory.json'
