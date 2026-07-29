# 시리즈 제목에서 권차 누락을 보수적으로 찾는 규칙을 검증합니다.
import unittest

from series_gap import find_series_gaps, parse_volume_markers


class SeriesGapTest(unittest.TestCase):
    def test_finds_middle_gap_and_ignores_book_id_suffix(self):
        rows = [
            {"id": 101, "library_id": 1, "library_name": "만화", "series_name": "테스트", "title": "테스트 01권#101"},
            {"id": 102, "library_id": 1, "library_name": "만화", "series_name": "테스트", "title": "테스트 02권#102"},
            {"id": 104, "library_id": 1, "library_name": "만화", "series_name": "테스트", "title": "테스트 04권#104"},
        ]

        result = find_series_gaps(rows)

        self.assertEqual(1, len(result))
        self.assertEqual([3], result[0]["missing"])
        self.assertEqual([1, 2, 4], result[0]["present"])
        self.assertEqual("high", result[0]["confidence"])

    def test_supports_ranges_without_treating_covered_volumes_as_missing(self):
        rows = [
            {"id": 1, "library_id": 1, "library_name": "만화", "series_name": "합본", "title": "합본 1-3권"},
            {"id": 2, "library_id": 1, "library_name": "만화", "series_name": "합본", "title": "합본 5권"},
        ]

        result = find_series_gaps(rows)

        self.assertEqual([4], result[0]["missing"])
        self.assertEqual([1, 2, 3, 5], result[0]["present"])

    def test_parenthesized_range_before_page_count_covers_both_volumes(self):
        parsed = parse_volume_markers(
            "박봉성-신이라 불리운 사나이 5부(26-27)#175",
            900,
            "/mnt/comics/신이라 불리운 사나이 5부/박봉성-신이라 불리운 사나이 5부(26-27)#175.zip",
        )

        self.assertEqual({26, 27}, parsed["volumes"])
        self.assertFalse(parsed["ambiguous"])

        rows = []
        for volume in range(1, 56):
            if volume == 27:
                continue
            marker = "(26-27)" if volume == 26 else f"{volume:02d}권"
            rows.append({
                "id": 900 + volume,
                "library_id": 4,
                "library_name": "04.만화-완결B",
                "series_name": "신이라 불리운 사나이 5부",
                "title": f"신이라 불리운 사나이 5부 {marker}#{120 + volume}",
            })

        self.assertEqual([], find_series_gaps(rows))

    def test_does_not_infer_volume_from_bare_numbers(self):
        self.assertEqual(set(), parse_volume_markers("잡지 2026년 7월호#77", 77)["volumes"])
        self.assertEqual([], find_series_gaps([
            {"id": 77, "library_id": 1, "series_name": "잡지", "title": "잡지 2026년 7월호#77"},
            {"id": 78, "library_id": 1, "series_name": "잡지", "title": "잡지 2026년 8월호#78"},
        ]))

    def test_e_episode_markers_cover_complete_webtoon_series(self):
        rows = []
        for episode in range(1, 51):
            book_id = 46 + episode
            title = f"회춘.E{episode:04d}.{episode}화 회춘#{book_id}" if episode <= 11 else f"회춘.E{episode:04d} 회춘#{book_id}"
            rows.append({
                "id": book_id,
                "library_id": 7,
                "library_name": "07.웹툰",
                "series_name": "회춘 [기안84]",
                "title": title,
                "file_path": f"/mnt/gds2/GDRIVE/READING/웹툰/하/회춘 [기안84]/{title}.cbz",
            })

        self.assertEqual([], find_series_gaps(rows))
        self.assertEqual({12}, parse_volume_markers(rows[11]["title"], rows[11]["id"], rows[11]["file_path"])["volumes"])

    def test_uses_file_path_when_title_has_no_episode_marker(self):
        parsed = parse_volume_markers(
            "회춘 특별편#47",
            47,
            "/mnt/webtoon/회춘.E0001.1화 회춘#47.cbz",
        )

        self.assertEqual({1}, parsed["volumes"])
        self.assertFalse(parsed["ambiguous"])

    def test_e_marker_has_priority_over_different_hwa_marker(self):
        parsed = parse_volume_markers("회춘.E0002.3화 회춘#52", 52)

        self.assertEqual({2}, parsed["volumes"])
        self.assertFalse(parsed["ambiguous"])

    def test_castle_e_sequence_is_complete_even_when_display_hwa_is_offset(self):
        rows = []
        for episode in range(1, 122):
            book_id = 1000 + episode
            display_hwa = max(1, episode - 1)
            title = f"캐슬.E{episode:04d}.{display_hwa}화#{book_id}"
            rows.append({
                "id": book_id,
                "library_id": 8,
                "library_name": "07.웹툰",
                "series_name": "캐슬 [정연]",
                "title": title,
                "file_path": f"/mnt/webtoon/캐슬 [정연]/{title}.cbz",
            })

        self.assertEqual([], find_series_gaps(rows))
        parsed = parse_volume_markers("캐슬.E0119.118화#98", 98)
        self.assertEqual({119}, parsed["volumes"])
        self.assertFalse(parsed["ambiguous"])

    def test_multiple_different_e_markers_remain_ambiguous(self):
        parsed = parse_volume_markers("캐슬.E0119.E0120.118화#98", 98)

        self.assertEqual(set(), parsed["volumes"])
        self.assertTrue(parsed["ambiguous"])

    def test_leading_bracket_number_has_priority_over_volume_number(self):
        rows = []
        for sequence in range(1, 10):
            book_id = 300 + sequence
            title = f"[{sequence:02d}] [마블] 어메이징 스파이더맨 : 신즈 라이징 Vol. {max(1, sequence - 5)}#{book_id}"
            rows.append({
                "id": book_id,
                "library_id": 6,
                "library_name": "06.만화-Marvel",
                "series_name": "[마블] 어메이징 스파이더맨(2018-2021)",
                "title": title,
                "file_path": f"/mnt/marvel/{title}.zip",
            })

        self.assertEqual([], find_series_gaps(rows))
        parsed = parse_volume_markers(rows[5]["title"], rows[5]["id"], rows[5]["file_path"])
        self.assertEqual({6}, parsed["volumes"])
        self.assertFalse(parsed["ambiguous"])

    def test_number_before_page_count_suffix_is_used_as_volume(self):
        parsed = parse_volume_markers(
            "바다의 무녀 02#95",
            95,
            "/mnt/comics/바다의 무녀/바다의 무녀 02#95.cbz",
        )

        self.assertEqual({2}, parsed["volumes"])
        self.assertFalse(parsed["ambiguous"])

    def test_mixed_labeled_and_page_count_titles_cover_complete_series(self):
        labeled = {1, 5, 6, 7, 8, 11, 12}
        rows = []
        for volume in range(1, 13):
            pages = {1: 117, 2: 95, 3: 96}.get(volume, 90 + volume)
            marker = f"{volume:02d}권" if volume in labeled else f"{volume:02d}"
            if volume == 1:
                marker += " (Scan by잠부포스)"
            title = f"바다의 무녀 {marker}#{pages}"
            rows.append({
                "id": 500 + volume,
                "library_id": 4,
                "library_name": "04.만화-완결B",
                "series_name": "바다의 무녀",
                "title": title,
                "file_path": f"/mnt/comics/바다의 무녀/{title}.cbz",
            })

        self.assertEqual([], find_series_gaps(rows))

    def test_page_count_rule_ignores_year_range_and_missing_volume(self):
        self.assertEqual(set(), parse_volume_markers("연감 2024#117", 900)["volumes"])
        self.assertEqual(set(), parse_volume_markers("합본 1-3#117", 901)["volumes"])
        self.assertEqual(set(), parse_volume_markers("바다의 무녀#117", 902)["volumes"])
        self.assertEqual(set(), parse_volume_markers("어메이징 스파이더맨(2018-2021)#294", 903)["volumes"])


if __name__ == "__main__":
    unittest.main()
