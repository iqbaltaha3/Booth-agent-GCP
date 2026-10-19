"""Executed-result contracts for voter-roll lists and percentage thresholds."""

import sqlite3
import unittest

from agents.result_contracts import (
    distinct_list_request, distinct_values, qualifying_shares,
    share_threshold_request, shares_match_source,
)


def fact(source_id, columns, rows, truncated=False):
    return {"source_id": source_id, "columns": columns, "rows": rows,
            "row_count": len(rows), "truncated": truncated}


class ResultContractTests(unittest.TestCase):
    def test_distinct_regions_require_contiguous_pages_and_final_short_page(self):
        question = "list all unique regions"
        self.assertEqual(distinct_list_request(question), "region")
        pages = [
            fact("q1", ["region"], [[f"Region {n:03}"] for n in range(80)], True),
            fact("q2", ["region"], [[f"Region {n:03}"] for n in range(80, 85)]),
        ]
        sources = [
            {"source_id": "q1", "sql": "SELECT DISTINCT region FROM voters ORDER BY region"},
            {"source_id": "q2", "sql": "SELECT DISTINCT region FROM voters ORDER BY region LIMIT -1 OFFSET 80"},
        ]
        self.assertEqual(distinct_values([{"facts": [pages[0]], "sources": sources}], "region")[1], False)
        values, complete = distinct_values([{"facts": pages, "sources": sources}], "region")
        self.assertTrue(complete)
        self.assertEqual(len(values), 85)
        sources[1]["sql"] = "SELECT DISTINCT region FROM voters ORDER BY region LIMIT -1 OFFSET 81"
        self.assertFalse(distinct_values([{"facts": pages, "sources": sources}], "region")[1])

    def test_percentage_filter_needs_full_grouped_counts_and_exact_coverage(self):
        contract = share_threshold_request("list regions where muslim population is more than 50%")
        self.assertEqual(contract, {"group_by": "region", "category": "Muslim", "greater_than_percent": 50.0})
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute("CREATE TABLE voters(region TEXT, religion TEXT)")
        connection.executemany("INSERT INTO voters VALUES (?, ?)", [
            ("A", "Muslim"), ("A", "Muslim"), ("A", "Hindu"),
            ("B", "Muslim"), ("B", "Hindu"),
            ("C", "Muslim"), ("C", "Hindu"), ("C", "Hindu"),
        ])
        sql = ("SELECT region, SUM(CASE WHEN religion='Muslim' THEN 1 ELSE 0 END) AS muslim_count, "
               "COUNT(*) AS total_count FROM voters GROUP BY region")
        rows = [["A", 2, 3], ["B", 1, 2], ["C", 1, 3]]
        result = {"facts": [fact("q1", ["region", "muslim_count", "total_count"], rows)],
                  "sources": [{"source_id": "q1", "sql": sql}]}
        qualified = qualifying_shares([result], contract)
        self.assertEqual(qualified, [("A", 2, 3)])
        self.assertTrue(shares_match_source(connection, contract, qualified))
        self.assertFalse(shares_match_source(connection, contract, [("A", 2, 2)]))
        self.assertFalse(shares_match_source(connection, contract, []))
        result["facts"][0]["truncated"] = True
        self.assertIsNone(qualifying_shares([result], contract))


if __name__ == "__main__":
    unittest.main()
