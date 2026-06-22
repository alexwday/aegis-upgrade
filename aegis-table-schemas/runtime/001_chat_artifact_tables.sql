CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS public.chat_conversations (
    conversation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL,
    conversation_title text,
    created_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_chat_conversations_user_updated
    ON public.chat_conversations (user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS public.chat_messages (
    message_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id uuid NOT NULL REFERENCES public.chat_conversations(conversation_id) ON DELETE CASCADE,
    run_uuid uuid,
    role text NOT NULL,
    content text NOT NULL,
    created_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT chat_messages_role_check CHECK (role = ANY (ARRAY['system', 'user', 'assistant', 'tool']))
);

CREATE INDEX IF NOT EXISTS idx_chat_messages_conversation_created
    ON public.chat_messages (conversation_id, created_at ASC);

CREATE INDEX IF NOT EXISTS idx_chat_messages_run_uuid
    ON public.chat_messages (run_uuid);

CREATE TABLE IF NOT EXISTS public.artifacts (
    artifact_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id uuid NOT NULL REFERENCES public.chat_conversations(conversation_id) ON DELETE CASCADE,
    run_uuid uuid,
    artifact_title text NOT NULL,
    artifact_type text NOT NULL,
    artifact_content text NOT NULL,
    artifact_references jsonb,
    created_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_artifacts_conversation_created
    ON public.artifacts (conversation_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_artifacts_run_uuid
    ON public.artifacts (run_uuid);

CREATE INDEX IF NOT EXISTS idx_artifacts_type
    ON public.artifacts (artifact_type);
