import unittest
from pathlib import Path

import main
import utils


NOTES_FOLDER = Path(utils.getConfig()["notesFolderPath"])


def line_containing(path, text):
    return next(line for line in path.read_text().splitlines() if text in line)


def text_between(path, start_text, end_text):
    text = path.read_text()
    start_index = text.index(start_text)
    end_index = text.index(end_text, start_index)
    return text[start_index:end_index]


class MathDelimiterTests(unittest.TestCase):
    def test_currency_in_price_manipulation_article_stays_literal(self):
        source_path = NOTES_FOLDER / "draft-making-price-manipulation-attacks-un-profitable.md"
        source_line = line_containing(source_path, "$100k+ per year")

        converted_line = main.convert_tex_dollar_delimiters(source_line)

        self.assertEqual(source_line, converted_line)

    def test_explicit_tex_in_decision_market_article_uses_mathjax_safe_delimiters(self):
        source_path = NOTES_FOLDER / "decision-market-challenges.md"
        source_line = line_containing(source_path, r"$\frac{\textsf{Long}^{\text{yes}}_i}{\textsf{Long}^{\text{no}}}$")

        converted_line = main.convert_tex_dollar_delimiters(source_line)

        self.assertNotIn(r"$\textsf", converted_line)
        self.assertIn(r"\\(\textsf{Long}^{\text{no}}\\)", converted_line)
        self.assertIn(r"\\(\frac{\textsf{Long}^{\text{yes}}_i}{\textsf{Long}^{\text{no}}}\\)", converted_line)

    def test_plain_currency_zero_in_math_heavy_article_stays_literal(self):
        source_path = NOTES_FOLDER / "decision-market-challenges.md"
        source_line = line_containing(source_path, "worth $0 due")

        converted_line = main.convert_tex_dollar_delimiters(source_line)

        self.assertIn("worth $0 due", converted_line)
        self.assertIn(r"\\(\textsf{Short}^{\text{yes}}\\)", converted_line)

    def test_display_math_dollar_delimiters_use_mathjax_safe_delimiters(self):
        source_path = NOTES_FOLDER / "adsb-index.md"
        source_block = text_between(source_path, "By definition,", "3. **Liquidity Events**")

        converted_block = main.convert_tex_dollar_delimiters(source_block)

        self.assertNotIn("$$", converted_block)
        self.assertIn(r"\\[", converted_block)
        self.assertIn(r"p(a) + p(b) + p(c) \;=\; 1.", converted_block)
        self.assertIn(r"\\]", converted_block)

    def test_blog_post_scan_ignores_notes_subdirectories(self):
        matching_paths = main.find_files_containing_string(
            str(NOTES_FOLDER),
            utils.getConfig()["blogPostIdentifierPostfix"],
        )

        self.assertTrue(matching_paths)
        self.assertTrue(
            all(Path(path).parent == NOTES_FOLDER for path in matching_paths)
        )
        self.assertNotIn(
            str(NOTES_FOLDER / ".agents/skills/scheduled-tweet-ideas/SKILL.md"),
            matching_paths,
        )


if __name__ == "__main__":
    unittest.main()
