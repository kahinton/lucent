-- Migration 118: Pre-authentication API key lookup under RLS.
--
-- Bearer-key verification must find a key before Lucent can infer its
-- organization. The general api_keys tenant policy correctly requires an
-- org GUC, so the pre-auth bootstrap needs a narrow SELECT-only carve-out.
CREATE POLICY p_api_keys_preauth_select ON public.api_keys
FOR SELECT
TO lucent_app
USING (
    current_setting('app.role', true) = 'system'
    AND current_setting('app.auth_context', true) = 'preauth'
);
