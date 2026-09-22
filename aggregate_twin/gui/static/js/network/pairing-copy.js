/** Operator-facing words for pairing modes and pool states, shared by graph, panel and table. */

export const MODES = [
  { id: 'pooled', label: 'Broadcast', text: 'Offer each agent to every instance. The first to accept gets it.' },
  { id: 'first_free', label: 'First free', text: 'Hand each agent to the instance that has waited longest.' },
  { id: 'gui', label: 'Manual', text: 'Agents wait until you pair them with an instance.' },
  { id: 'auction', label: 'Auction', text: 'Free instances bid for each agent. The lightest load wins.' },
];

export const MODE_LABELS = Object.fromEntries(MODES.map(m => [m.id, m.label]));

const list = names => names.join(', ');

export const POOL_STATUS = {
  review: { short: 'needs review', tone: 'warn', long: () => 'needs review' },
  waiting: {
    short: 'waiting', tone: 'muted',
    long: (_, pairing) => (pairing.mode === 'gui' ? 'waiting for you to pair it' : 'waiting for a free instance'),
  },
  chosen: { short: 'your pick', tone: 'goal', long: a => `your pick ${a.choice} is still busy` },
  requesting: { short: 'asking all', tone: 'info', long: () => 'asking every instance' },
  offered: { short: 'offered', tone: 'info', long: a => `offered to ${list(a.targets)}` },
  bidding: { short: 'bidding', tone: 'info', long: a => `${a.targets.length} instances bidding` },
  releasing: { short: 'releasing', tone: 'muted', long: () => 'releasing its handshake' },
  free: { short: 'free', tone: 'ok', long: () => 'free' },
  engaged: { short: 'busy', tone: 'info', long: i => `in a handshake with ${list(i.engaged_with)}` },
};
