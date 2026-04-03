import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import reflex_reviewer.refine as refine_module
from reflex_reviewer.refine import run_training_cycle


class RefineTeamNameSanitizationTests(unittest.TestCase):
    @patch("reflex_reviewer.refine.create_fine_tune_job", return_value="job_1")
    @patch("reflex_reviewer.refine.upload_file")
    def test_run_training_cycle_sanitizes_team_name_for_suffix(
        self,
        mock_upload_file,
        mock_create_fine_tune_job,
    ):
        mock_upload_file.side_effect = ["file_train", "file_val"]

        run_training_cycle(
            train_path="train.jsonl",
            val_path="val.jsonl",
            draft_model="oca/gpt-4.1",
            team_name=" Team/DEV-OPS "
        )

        self.assertEqual(
            mock_create_fine_tune_job.call_args.kwargs.get("suffix"),
            "team-dev-ops",
        )

    @patch("reflex_reviewer.refine.upload_file")
    def test_run_training_cycle_rejects_team_name_without_alphanumeric_chars(
        self,
        mock_upload_file,
    ):
        with self.assertRaisesRegex(ValueError, "at least one alphanumeric"):
            run_training_cycle(
                train_path="train.jsonl",
                val_path="val.jsonl",
                draft_model="oca/gpt-4.1",
                team_name="---",
            )

        mock_upload_file.assert_not_called()


class RefineSplitFilePathTests(unittest.TestCase):
    @patch("reflex_reviewer.refine.wait_for_fine_tune_completion")
    @patch("reflex_reviewer.refine.run_training_cycle", return_value="job_1")
    def test_run_writes_train_and_val_files_under_dpo_training_data_dir(
        self,
        mock_run_training_cycle,
        mock_wait_for_fine_tune_completion,
    ):
        mock_wait_for_fine_tune_completion.return_value = {"status": "failed"}

        with TemporaryDirectory() as temp_dir:
            dataset_path = Path(temp_dir) / "team_dpo_training_data.jsonl"
            dataset_path.write_text('{"sample": 1}\n{"sample": 2}\n', encoding="utf-8")

            with patch.object(refine_module, "MIN_SAMPLES_TO_TRAIN", 1), patch.object(
                refine_module, "TRAIN_SPLIT_RATIO", 0.5
            ):
                refine_module.run(
                    dpo_training_data_dir=temp_dir,
                    team_name="TEAM",
                    draft_model="oca/gpt-4.1",
                    stream_response=False,
                )

            expected_train_path = str(Path(temp_dir) / "train.jsonl")
            expected_val_path = str(Path(temp_dir) / "val.jsonl")

            self.assertTrue(Path(expected_train_path).is_file())
            self.assertTrue(Path(expected_val_path).is_file())
            mock_run_training_cycle.assert_called_once_with(
                expected_train_path,
                expected_val_path,
                "oca/gpt-4.1",
                "TEAM",
            )


if __name__ == "__main__":
    unittest.main()