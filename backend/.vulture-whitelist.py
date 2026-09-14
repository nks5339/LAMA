# Vulture whitelist — justified survivors.
#
# Run the dead-code bar with:
#
#     .venv/bin/vulture backend backend/.vulture-whitelist.py \
#         --min-confidence 80 --exclude backend/tests
#
# It must report nothing. Vulture reports unused PARAMETERS as "unused
# variable", so the entries here are the same signature-dictated parameters
# already reasoned about in backend/ruff.toml under [lint.per-file-ignores].
# The reason lives there; this file only re-states the names so the two tools
# agree on the bar.
#
# Anything not listed here must be removed rather than added.

# generate_oltp_ddl(..., data_model_hints) — uniform generator signature
# across oltp / olap / bus_matrix.
data_model_hints

# _split_on_structure(..., max_piece) — shared signature with the size-based
# splitter beside it.
max_piece

# _build_deterministic_root_content(..., dominant_lang) — shared shape across
# the per-language branches.
dominant_lang

# FakeCollection.find_one(self, flt, projection=None) in the in-memory test
# fixtures — Motor's real signature is find_one(filter, projection=None) and
# production calls it WITH a projection (e.g. routes/pipeline.py passes
# {"_id": 0}). The parameter has to be accepted even though the fakes ignore
# it, or every such call raises TypeError. 7 occurrences across
# test_iter1410 / test_iter1411 / test_iter149 / test_iter1545 /
# test_srs_streaming.
projection
