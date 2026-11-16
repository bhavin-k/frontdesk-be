"""Check the database role before calling recruiter-only authorization functions."""

from alembic import op

revision = "0013a_recruiter_decision_guard"
down_revision = "0013_experience_first_matching"
branch_labels = None
depends_on = None


def _guard(check_role_first: bool) -> None:
    role_denied = "current_user <> 'recruitment_api'"
    reject = "Explicit recruiter decision required"
    select = "Explicit recruiter selection required"

    def require_api(message: str) -> str:
        if not check_role_first:
            return ""
        return f"""
            IF {role_denied} THEN
                RAISE EXCEPTION '{message}' USING ERRCODE='42501';
            END IF;
        """

    role_condition = "" if check_role_first else f"{role_denied} OR "
    op.execute(f"""
        CREATE OR REPLACE FUNCTION public.protect_rejection() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$ BEGIN
            IF TG_OP='UPDATE' AND OLD.status='rejected'
                AND current_user IN ('recruitment_api','recruitment_worker') AND (
                    NEW.status IS DISTINCT FROM OLD.status
                    OR NEW.rejected_at IS DISTINCT FROM OLD.rejected_at
                    OR NEW.latest_match_id IS DISTINCT FROM OLD.latest_match_id
                    OR NEW.latest_evaluation_id IS DISTINCT FROM OLD.latest_evaluation_id) THEN
                RAISE EXCEPTION 'Confirmed rejection is immutable' USING ERRCODE='42501';
            END IF;
            IF NEW.status='rejected' AND (TG_OP='INSERT' OR OLD.status IS DISTINCT FROM NEW.status)
            THEN
                {require_api(reject)}
                IF {role_condition}NOT public.tenant_is_member(NEW.tenant_id)
                    OR NOT EXISTS(SELECT 1 FROM public.review_decisions
                        WHERE tenant_id=NEW.tenant_id AND application_id=NEW.id
                        AND ((NEW.latest_evaluation_id IS NULL AND match_id=NEW.latest_match_id)
                            OR (evaluation_id=NEW.latest_evaluation_id AND EXISTS(
                                SELECT 1 FROM public.matching_evaluations e WHERE e.id=evaluation_id
                                AND e.application_id=NEW.id AND e.resume_id=NEW.current_resume_id
                                AND e.state='completed')))
                        AND recruiter_id=public.actor_context() AND decision='reject'
                        AND created_at >= transaction_timestamp()) THEN
                    RAISE EXCEPTION '{reject}' USING ERRCODE='42501';
                END IF;
            END IF;
            IF NEW.status='shortlisted' AND NEW.latest_evaluation_id IS NOT NULL
                AND (TG_OP='INSERT' OR OLD.status IS DISTINCT FROM NEW.status
                     OR OLD.opening_id IS DISTINCT FROM NEW.opening_id) THEN
                {require_api(select)}
                IF {role_condition}NOT public.tenant_is_member(NEW.tenant_id) OR NOT EXISTS(
                    SELECT 1 FROM public.review_decisions d
                    JOIN public.role_assessments r ON r.id=d.role_assessment_id
                        AND r.evaluation_id=d.evaluation_id AND r.tenant_id=d.tenant_id
                    WHERE d.tenant_id=NEW.tenant_id AND d.application_id=NEW.id
                    AND d.evaluation_id=NEW.latest_evaluation_id
                    AND r.opening_id=NEW.opening_id AND d.decision='shortlist'
                    AND d.recruiter_id=public.actor_context()
                    AND d.created_at >= transaction_timestamp()
                ) THEN
                    RAISE EXCEPTION '{select}' USING ERRCODE='42501';
                END IF;
            END IF;
            RETURN NEW;
        END $$;
    """)


def upgrade() -> None:
    _guard(check_role_first=True)


def downgrade() -> None:
    _guard(check_role_first=False)
