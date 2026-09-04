import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import frontmatter

import main
import utils


NOTES_FOLDER = Path(utils.getConfig()["notesFolderPath"])


def line_containing(path, text):
    return next(line for line in path.read_text().splitlines() if text in line)


def formatted_source_content(source_path):
    config = utils.getConfig()
    publication_markers = (
        config["blogPostIdentifierPostfix"],
        config["hiddenPostPostfix"],
    )
    published_paths = []
    for publication_marker in publication_markers:
        published_paths.extend(
            main.find_files_containing_string(str(NOTES_FOLDER), publication_marker)
        )
    published_names = [Path(path).name for path in published_paths]

    with tempfile.TemporaryDirectory() as temporary_directory:
        test_path = Path(temporary_directory) / source_path.name
        shutil.copyfile(source_path, test_path)
        with mock.patch.multiple(
            main,
            create=True,
            contactInfo=config["contactInfo"],
            postPostfix=config["blogPostIdentifierPostfix"],
            hiddenPostPostfix=config["hiddenPostPostfix"],
        ):
            main.formatPostContents(str(test_path), published_names)
        return frontmatter.load(test_path).content


class MathDelimiterTests(unittest.TestCase):
    def test_currency_code_spans_pass_through_formatting(self):
        source_path = NOTES_FOLDER / "actually-impact-futures.md"
        source_line = line_containing(source_path, "Probability error amplification")

        formatted_content = formatted_source_content(source_path)

        self.assertIn(source_line, formatted_content)

    def test_explicit_inline_math_and_literal_currency_pass_through_formatting(self):
        source_path = NOTES_FOLDER / "decision-market-challenges.md"
        source_line = line_containing(source_path, "worth $0 due")

        formatted_content = formatted_source_content(source_path)

        self.assertIn(source_line, formatted_content)
        self.assertIn(r"\\(\textsf{Short}^{\text{yes}}\\)", formatted_content)

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

    def test_published_note_overrides_legacy_unpublished_metadata(self):
        source_path = NOTES_FOLDER / "actually-impact-futures.md"

        with tempfile.TemporaryDirectory() as temporary_directory:
            test_path = Path(temporary_directory) / source_path.name
            shutil.copyfile(source_path, test_path)
            post = frontmatter.load(test_path)
            post["hidden"] = True
            post["published"] = False
            frontmatter.dump(post, test_path)

            with mock.patch.multiple(
                main,
                create=True,
                postPostfix=utils.getConfig()["blogPostIdentifierPostfix"],
                hiddenPostPostfix=utils.getConfig()["hiddenPostPostfix"],
            ):
                main.add_frontmatter(
                    str(test_path),
                    date="2026-02-03",
                    description=post["description"],
                    articleUrl=post["articleUrl"],
                    isHidden=False,
                )

            normalized_post = frontmatter.load(test_path)
            self.assertFalse(normalized_post["hidden"])
            self.assertTrue(normalized_post["published"])


if __name__ == "__main__":
    unittest.main()
