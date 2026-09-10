# Scan Queue

The queue displays one compact row per brand. All category campaigns for a brand share its manually selected tier. Regular campaigns retain a turn each rotation; less regular campaigns wait four nominal rotations between attempts. Token limits and the existing scan lock still apply. Existing brands default to regular.

Brand Details contains scan status, latest BUY/CONSIDER counts per ASIN, recent automated scan results, pending recommendations, tier changes and removal. Removing a brand removes its category campaigns, not historical product records. Add brand is on the queue page.

The background queue scheduler saves a weekly snapshot of tier recommendations from the existing Attention Engine, which combines scan results, own EU A2A buying history and competitor evidence. Suggestions never alter scanning until approved. A manual tier choice supersedes pending suggestions; dismissed suggestions do not return within the same week. The Command Centre links to pending decisions. Automatic extra-attention scans no longer override the saved cadence.

New tables are created by the existing startup schema initialization. No existing product or reporting data is rewritten. Filtered catalogue counts are observed when the final Product Finder page is reached; dates are recorded only after a traversal tracked from page zero. Separate category campaign totals are not added because they can overlap. Unknown historical totals and dates display as not recorded.

Product Finder filters and Replen eligibility rules remain in their existing services. More info links to the recorded VA/retailer source or the source Amazon marketplace; unknown sources are labelled as missing.

Validation: `python -m unittest test_scan_queue_redesign test_command_centre_counts test_va_actuals test_va_price_drop_audit test_va_submission_edits` from backend. Tests use isolated databases and mocked external scans.
