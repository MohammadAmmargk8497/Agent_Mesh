// AgentMesh boundary adapter: no changes to OpenHuddle's authentication or schema.
export function seedHub(hub, tokenHashes = {}) {
  for (const name of ['agent-a', 'agent-b']) {
    if (!/^[a-f0-9]{64}$/.test(tokenHashes[name] || '')) throw new Error(`Missing seed hash for ${name}`);
  }
  hub.db.exec('CREATE TABLE IF NOT EXISTS agentmesh_seed (name TEXT PRIMARY KEY, team_id TEXT NOT NULL, member_id TEXT NOT NULL)');
  return hub.tx(() => {
    const seeded = hub.get('SELECT team_id FROM agentmesh_seed LIMIT 1');
    if (!seeded && !hub.get('SELECT id FROM teams LIMIT 1')) {
      const owner = hub.createTeam({ team_name: 'default', member_name: 'agent-a' });
      const { member } = hub.addMember(owner.team_id, { name: 'agent-b', role: 'member' });
      for (const [name, id] of [['agent-a', owner.member_id], ['agent-b', member.id]]) {
        hub.run('INSERT INTO agentmesh_seed (name, team_id, member_id) VALUES (?, ?, ?)', name, owner.team_id, id);
      }
    }
    // Reconcile only the originally seeded, still-active memberships. Never resurrect
    // a removed member or deleted team, or change a newly created team's credentials.
    for (const [name, hash] of Object.entries(tokenHashes)) {
      const seed = hub.get('SELECT member_id FROM agentmesh_seed WHERE name = ?', name);
      if (seed) hub.run('UPDATE members SET token_hash = ? WHERE id = ? AND removed_at IS NULL AND token_hash != ?', hash, seed.member_id, hash);
    }
    return hub.get('SELECT team_id FROM agentmesh_seed LIMIT 1')?.team_id ?? null;
  });
}
