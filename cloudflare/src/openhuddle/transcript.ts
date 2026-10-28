// Read-only projection of real messages, not generated activity/status text.
export function teamTranscript(sql: SqlStorage, teamId: string) {
  const rows = sql.exec<{
    id: string; created_at: number; sender: string; recipient: string;
    kind: string; text: string; ask_id: string | null; huddle_id: string | null;
    task_id: string | null; prompt_version: number | null; order_id: number;
  }>(`
    WITH scope AS (SELECT ? AS team_id), entries AS (
      SELECT 'ask:' || a.id AS id, a.created_at, a.from_id AS sender_id, a.to_id AS recipient_id,
        'ask' AS kind, a.question AS text, a.id AS ask_id, NULL AS huddle_id,
        NULL AS task_id, NULL AS prompt_version,
        COALESCE((SELECT MIN(e.id) FROM events e WHERE e.team_id=a.team_id AND e.kind='ask'
          AND json_extract(e.data, '$.ask_id')=a.id), 0) AS order_id
      FROM asks a JOIN scope s ON s.team_id=a.team_id
      UNION ALL
      SELECT 'event:' || e.id, e.created_at, e.member_id, json_extract(e.data, '$.to_id'),
        'answer', json_extract(e.data, '$.answer'), json_extract(e.data, '$.ask_id'), NULL,
        NULL, NULL, e.id
      FROM events e JOIN scope s ON s.team_id=e.team_id WHERE e.kind='reply'
      UNION ALL
      SELECT 'answer:' || a.id, a.answered_at, a.to_id, a.from_id,
        'answer', a.answer, a.id, NULL, NULL, NULL,
        COALESCE((SELECT MIN(e.id) FROM events e WHERE e.team_id=a.team_id AND e.kind='ask'
          AND json_extract(e.data, '$.ask_id')=a.id), 0) + 0.5
      FROM asks a JOIN scope s ON s.team_id=a.team_id
      WHERE a.answer IS NOT NULL AND a.answered_at IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM events e WHERE e.team_id=a.team_id AND e.kind='reply'
          AND json_extract(e.data, '$.ask_id')=a.id)
      UNION ALL
      SELECT 'huddle:' || p.huddle_id || ':' || p.seq, p.created_at, p.member_id, NULL,
        CASE WHEN p.kind='comment' THEN 'huddle comment' ELSE 'huddle post' END,
        p.text, NULL, p.huddle_id, NULL, NULL,
        COALESCE((SELECT MIN(e.id) FROM events e WHERE e.team_id=p.team_id AND e.kind='huddle_post'
          AND json_extract(e.data, '$.huddle_id')=p.huddle_id
          AND json_extract(e.data, '$.seq')=p.seq), 0)
      FROM huddle_posts p JOIN scope s ON s.team_id=p.team_id
      UNION ALL
      SELECT 'event:' || e.id, e.created_at, e.member_id, NULL,
        'huddle post', json_extract(e.data, '$.text'), NULL, NULL, NULL, NULL, e.id
      FROM events e JOIN scope s ON s.team_id=e.team_id WHERE e.kind IN ('update', 'decision')
      UNION ALL
      SELECT 'event:' || e.id, e.created_at, e.member_id, NULL,
        'task note', json_extract(e.data, '$.note'), NULL, NULL,
        json_extract(e.data, '$.key'), NULL, e.id
      FROM events e JOIN scope s ON s.team_id=e.team_id WHERE e.kind GLOB 'task_*'
      UNION ALL
      SELECT 'event:' || e.id, e.created_at, e.member_id, NULL,
        'master-prompt', json_extract(e.data, '$.comment'), NULL, NULL, NULL,
        json_extract(e.data, '$.version'), e.id
      FROM events e JOIN scope s ON s.team_id=e.team_id WHERE e.kind='prompt_vote'
      UNION ALL
      SELECT 'event:' || e.id, e.created_at, e.member_id, NULL,
        'master-prompt', json_extract(section.value, '$.body'), NULL, NULL, NULL, p.version, e.id
      FROM events e JOIN scope s ON s.team_id=e.team_id
      JOIN prompt_versions p ON p.team_id=e.team_id AND p.version=json_extract(e.data, '$.version')
      JOIN json_each(p.sections) section ON json_extract(section.value, '$.title')=json_extract(e.data, '$.section')
      WHERE e.kind='prompt_edit'
    )
    SELECT x.id, x.created_at, COALESCE(sender.name, x.sender_id, 'team') AS sender,
      COALESCE(recipient.name, x.recipient_id, 'team') AS recipient,
      x.kind, x.text, x.ask_id, x.huddle_id, x.task_id, x.prompt_version, x.order_id
    FROM entries x CROSS JOIN scope s
    LEFT JOIN members sender ON sender.team_id=s.team_id AND sender.id=x.sender_id
    LEFT JOIN members recipient ON recipient.team_id=s.team_id AND recipient.id=x.recipient_id
    WHERE typeof(x.text)='text' AND x.text<>'' AND x.created_at IS NOT NULL
    ORDER BY x.created_at DESC, x.order_id DESC, x.id DESC LIMIT 500
  `, teamId).toArray();
  return rows.reverse().map(({ order_id, ...row }) => row);
}
