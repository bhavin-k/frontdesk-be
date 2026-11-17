# Job openings

Save an opening with a title and job description. Enter any additional requirements and a minimum total experience in the opening form. A blank minimum means there is no numeric floor. Supporting files and interview questions remain available to recruiters.

Each accepted CV is parsed once and matched against the openings active in its business at the time of arrival. Email intake uses the provider's receipt timestamp. The worker saves one result per CV and opening. Changes to a JD apply to future CVs. Creating an opening does not queue existing CVs.

Pause an opening to stop accepting new uploads for it. Mail received during its pause is skipped even if delivered to the worker after you continue the opening. Closing preserves the previous intake history and excludes the opening from future CVs. A recruiter can select an active opening from the saved assessment, or confirm a rejection in review.

The opening API accepts `title`, `job_description`, optional `details`, `questions` and supporting files. Edits and lifecycle actions require the current `expected_revision`. Refresh if another recruiter changed the opening while you were editing.
