# Exporting completed physical sessions

1. Re-run `analysis/generate_summary.py` from the archived measurement session
   and `analysis/profile_tools.py summarize` from the profile session.
2. Run `analysis/publish_results.py MEASUREMENT PROFILE OUTPUT`. It rejects
   loopback data, missing five-repetition tuples, and incomplete architecture
   profiles.
3. Independently recompute the selected row in `claim_evidence.csv`. Only a row
   with `claim_passes=true` may be copied to the README/resume.
4. Archive raw sessions and `perf.data` outside Git. Retain the generated
   checksum manifest and archive location.
5. Commit the cleaned CSV, evidence CSV, exactly two figures, environment,
   profile reports, methodology, checksums, and narrative. Keep the benchmark
   commit distinct from this publication commit.
