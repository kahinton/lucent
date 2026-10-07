-- Migration 124: Group-owner clearances for secrets.
--
-- Migration 123's owner triggers grant clearances to the *user* owner column
-- only. Secrets additionally support group ownership (owner_user_id IS NULL
-- AND owner_group_id IS NOT NULL): the group's members read the secret, while
-- modification stays gated by group-admin checks at the application layer.
-- Give the owning group principal a 'read' clearance so clearance-driven
-- listing sees group-owned secrets; write access keeps its legacy checks.

CREATE OR REPLACE FUNCTION public.grant_auth_group_clearance_for_row()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    IF NEW.owner_group_id IS NULL OR NEW.owner_user_id IS NOT NULL THEN
        RETURN NEW;
    END IF;

    INSERT INTO auth_clearances
        (auth_id, role, principal_type, principal_id)
    VALUES
        (NEW.auth_id, 'read', 'group', NEW.owner_group_id)
    ON CONFLICT (auth_id, principal_type, principal_id, role)
    DO NOTHING;

    RETURN NEW;
END;
$$;

CREATE TRIGGER auth_group_clearance
AFTER INSERT ON public.secrets
FOR EACH ROW EXECUTE FUNCTION public.grant_auth_group_clearance_for_row();

-- Backfill: existing group-owned secrets. (The upsert path only UPDATEs rows
-- whose ownership columns already match the caller's SecretScope, so owner
-- columns never change on UPDATE and an INSERT trigger suffices.)
INSERT INTO auth_clearances
    (auth_id, role, principal_type, principal_id)
SELECT secrets.auth_id, 'read', 'group', secrets.owner_group_id
FROM public.secrets
WHERE secrets.owner_group_id IS NOT NULL
  AND secrets.owner_user_id IS NULL
  AND secrets.auth_id IS NOT NULL
ON CONFLICT (auth_id, principal_type, principal_id, role)
DO NOTHING;