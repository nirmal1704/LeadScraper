'use client';
import { useEffect, useRef, useState } from 'react';
import { useRouter } from 'next/navigation';
import { onAuthStateChanged, signOut, User } from 'firebase/auth';
import { collection, doc, onSnapshot } from 'firebase/firestore';
import { auth, db } from '@/lib/firebase';
import { startJob, stopJob, downloadExcel } from '@/lib/api';

// ── Types ─────────────────────────────────────────────────────────────────────
interface Job {
  id: string;
  status: 'queued' | 'running' | 'done' | 'error' | 'stopped';
  query: string;
  logs: string[];
  leads_count?: number;
  hot_count?: number;
  warm_count?: number;
  download_url?: string;
  plan?: { cities: string[]; search_queries: string[]; sources: string[] };
}

interface Lead {
  id: string;
  name: string;
  city: string;
  phone?: string;
  website?: string;
  website_domain?: string;
  instagram_handle?: string;
  score: number;
  priority: 'Hot' | 'Warm' | 'Medium' | 'Cold' | 'Skip';
  query: string;
  area?: string;
  lead_type?: string;
  confidence?: number;
  evidence?: string;
  address?: string;
  google_maps_url?: string;
  social_links?: string;
}


const PRIORITY_ORDER: Record<string, number> = { Hot: 0, Warm: 1, Medium: 2, Cold: 3, Skip: 4 };

function messageFromError(error: unknown, fallback: string) {
  return error instanceof Error ? error.message : fallback;
}

// ── Main Page ─────────────────────────────────────────────────────────────────
export default function Dashboard() {
  const router = useRouter();
  const [user, setUser] = useState<User | null>(null);
  const [queryText, setQueryText] = useState('');
  const [currentJobId, setCurrentJobId] = useState<string | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [leads, setLeads] = useState<Lead[]>([]);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState('');
  const logRef = useRef<HTMLDivElement>(null);
  const prevLogLen = useRef(0);

  // Auth guard
  useEffect(() => {
    const unsub = onAuthStateChanged(auth, (u) => {
      if (!u) router.replace('/login');
      else setUser(u);
    });
    return unsub;
  }, [router]);

  // Real-time job listener (Firestore onSnapshot — no polling)
  useEffect(() => {
    if (!user || !currentJobId) return;
    const jobRef = doc(db, 'users', user.uid, 'jobs', currentJobId);
    const unsub = onSnapshot(jobRef, (snap) => {
      if (snap.exists()) setJob({ id: snap.id, ...snap.data() } as Job);
    });
    return unsub;
  }, [user, currentJobId]);

  // Real-time leads listener
  useEffect(() => {
    if (!user || !currentJobId) return;
    const leadsCol = collection(db, 'users', user.uid, 'jobs', currentJobId, 'leads');
    const unsub = onSnapshot(leadsCol, (snap) => {
      const items = snap.docs.map(d => ({ id: d.id, ...d.data() } as Lead));
      items.sort((a, b) => {
        const priority = (PRIORITY_ORDER[a.priority] ?? 5) - (PRIORITY_ORDER[b.priority] ?? 5);
        if (priority !== 0) return priority;
        return (b.score || 0) - (a.score || 0);
      });
      setLeads(items);
    });
    return unsub;
  }, [user, currentJobId]);

  // Auto-scroll log to bottom only when new lines appear
  useEffect(() => {
    const logs = job?.logs || [];
    if (logs.length > prevLogLen.current) {
      prevLogLen.current = logs.length;
      if (logRef.current) {
        logRef.current.scrollTop = logRef.current.scrollHeight;
      }
    }
  }, [job?.logs]);

  const handleStart = async () => {
    if (!queryText.trim() || !user) return;
    setError('');
    setStarting(true);
    setLeads([]);
    setJob(null);
    prevLogLen.current = 0;
    try {
      const token = await user.getIdToken();
      const { job_id } = await startJob(token, queryText.trim(), []);
      setCurrentJobId(job_id);
    } catch (e: unknown) {
      setError(messageFromError(e, 'Failed to start. Is the backend running?'));
    } finally {
      setStarting(false);
    }
  };

  const handleStop = async () => {
    if (!currentJobId || !user) return;
    const token = await user.getIdToken();
    await stopJob(token, currentJobId);
  };

  const handleDownload = async () => {
    if (!currentJobId || !user) return;
    try {
      const token = await user.getIdToken();
      await downloadExcel(token, currentJobId);
    } catch (e: unknown) {
      setError(messageFromError(e, 'Failed to download.'));
    }
  };



  const isRunning = job?.status === 'running' || job?.status === 'queued';
  const isDone = job?.status === 'done' || job?.status === 'stopped';

  // Anti-Sleep Heartbeat: Ping the backend every 2 minutes while a job is running 
  // so Render's free tier doesn't put the server to sleep!
  useEffect(() => {
    let interval: NodeJS.Timeout;
    if (isRunning) {
      interval = setInterval(() => {
        fetch(`${process.env.NEXT_PUBLIC_BACKEND_URL || 'http://localhost:8000'}/health`)
          .catch(() => {}); // ignore errors silently
      }, 120000); // 2 minutes
    }
    return () => {
      if (interval) clearInterval(interval);
    };
  }, [isRunning]);

  if (!user) return null;

  return (
    <div style={{ maxWidth: 1100, margin: '0 auto', padding: '32px 24px' }}>

      {/* Header */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 40 }}>
        <div>
          <h1 style={{ fontSize: 20, fontWeight: 600 }}>LeadScraper</h1>
          <p style={{ color: 'var(--muted)', fontSize: 13 }}>
            Find businesses without websites. Pitch them one.
          </p>
        </div>
        <button
          className="btn btn-ghost"
          style={{ fontSize: 13, padding: '8px 14px' }}
          onClick={() => signOut(auth)}
        >
          Sign out
        </button>
      </div>

      {/* Search Form */}
      <div className="card" style={{ marginBottom: 24 }}>
        <label style={{ display: 'block', fontSize: 12, color: 'var(--muted)', marginBottom: 8, fontWeight: 500, letterSpacing: '0.04em', textTransform: 'uppercase' }}>
          What are you looking for?
        </label>
        <textarea
          rows={3}
          placeholder="e.g. yoga and dance studios in Mumbai and Pune, small local businesses only"
          value={queryText}
          onChange={e => setQueryText(e.target.value)}
          disabled={isRunning}
          style={{ resize: 'none', marginBottom: 20, lineHeight: 1.7 }}
        />



        <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
          <button
            className="btn btn-primary"
            onClick={handleStart}
            disabled={isRunning || starting || !queryText.trim()}
            style={{ minWidth: 120 }}
          >
            {starting ? 'Starting...' : isRunning ? 'Running...' : 'Start'}
          </button>
          {isRunning && (
            <button className="btn btn-danger" onClick={handleStop}>
              Stop
            </button>
          )}
          {error && <span style={{ color: 'var(--hot)', fontSize: 13 }}>{error}</span>}
        </div>
      </div>

      {/* Progress + Stats */}
      {job && (
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 24, marginBottom: 24 }}>

          {/* Log feed */}
          <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
            <div style={{ padding: '14px 20px', borderBottom: '1px solid var(--border)', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <span style={{ fontSize: 13, fontWeight: 500 }}>Progress</span>
              <StatusBadge status={job.status} />
            </div>
            <div
              ref={logRef}
              style={{ height: 280, overflowY: 'auto', padding: '12px 20px' }}
            >
              {(job.logs || []).map((line, i) => (
                <div
                  key={i}
                  className={`log-line ${i >= (job.logs.length - 3) ? 'new' : ''}`}
                >
                  {line}
                </div>
              ))}
              {!job.logs?.length && (
                <p style={{ color: 'var(--muted)', fontSize: 12 }}>Waiting to start...</p>
              )}
            </div>
          </div>

          {/* Stats */}
          <div className="card">
            <p style={{ fontSize: 12, color: 'var(--muted)', marginBottom: 16, fontWeight: 500, textTransform: 'uppercase', letterSpacing: '0.04em' }}>
              Results
            </p>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16, marginBottom: 24 }}>
              <Stat label="Total leads" value={leads.length} />
              <Stat label="Hot" value={leads.filter(l => l.priority === 'Hot').length} color="var(--hot)" />
              <Stat label="Warm" value={leads.filter(l => l.priority === 'Warm').length} color="var(--warm)" />
              <Stat label="Medium" value={leads.filter(l => l.priority === 'Medium').length} color="var(--medium)" />
            </div>
            {isDone && leads.length > 0 && (
              <button
                onClick={handleDownload}
                className="btn btn-primary"
                style={{ width: '100%', textDecoration: 'none' }}
              >
                Download Excel
              </button>
            )}
            {isDone && leads.length === 0 && (
              <p style={{ color: 'var(--muted)', fontSize: 13 }}>No leads found — try a different query.</p>
            )}
            {job.plan && (
              <p style={{ color: 'var(--muted)', fontSize: 12, marginTop: 16, lineHeight: 1.6 }}>
                Searching {job.plan.cities?.join(', ')}
              </p>
            )}
          </div>
        </div>
      )}

      {/* Leads Table */}
      {leads.length > 0 && (
        <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
          <div style={{ padding: '14px 20px', borderBottom: '1px solid var(--border)' }}>
            <span style={{ fontSize: 13, fontWeight: 500 }}>
              Leads — sorted by priority
            </span>
            <span style={{ color: 'var(--muted)', fontSize: 12, marginLeft: 12 }}>
              {leads.length} total
            </span>
          </div>
          <div style={{ overflowX: 'auto' }}>
            <table className="leads-table">
              <thead>
                <tr>
                  <th>Priority</th>
                  <th>Business</th>
                  <th>City</th>
                  <th>Area</th>
                  <th>Phone</th>
                  <th>Type</th>
                  <th>Website</th>
                  <th>Other Links</th>
                  <th>Instagram</th>
                  <th>Confidence</th>
                  <th>Score</th>
                  <th>Maps</th>
                </tr>
              </thead>
              <tbody>
                {leads.slice(0, 200).map(lead => (
                  <tr key={lead.id}>
                    <td><PriorityBadge p={lead.priority} /></td>
                    <td style={{ fontWeight: 500, color: 'var(--text)' }}>{lead.name}</td>
                    <td style={{ color: 'var(--muted)' }}>{lead.city}</td>
                    <td style={{ color: 'var(--muted)' }}>{lead.area || '-'}</td>
                    <td>{lead.phone || <span style={{ color: 'var(--muted)' }}>-</span>}</td>
                    <td title={lead.evidence || ''}>{lead.lead_type || '-'}</td>
                    <td>
                      {lead.website
                        ? <a href={lead.website} target="_blank" rel="noreferrer" title={lead.website} style={{ color: 'var(--accent)', textDecoration: 'none' }}>{lead.website_domain || 'Open'}</a>
                        : <span style={{ color: 'var(--hot)', fontSize: 12 }}>None</span>}
                    </td>
                    <td title={lead.social_links || ''}>
                      {lead.social_links
                        ? <span style={{ color: 'var(--muted)', fontSize: 11 }}>{lead.social_links.split(',').length} links</span>
                        : <span style={{ color: 'var(--muted)' }}>—</span>}
                    </td>
                    <td>
                      {lead.instagram_handle
                        ? <a href={`https://instagram.com/${lead.instagram_handle}`} target="_blank" rel="noreferrer" style={{ color: 'var(--accent)', textDecoration: 'none' }}>@{lead.instagram_handle}</a>
                        : <span style={{ color: 'var(--muted)' }}>—</span>}
                    </td>
                    <td>{lead.confidence ? `${lead.confidence}%` : <span style={{ color: 'var(--muted)' }}>-</span>}</td>
                    <td style={{ fontWeight: 600 }}>{lead.score}</td>
                    <td>
                      {lead.google_maps_url
                        ? <a href={lead.google_maps_url} target="_blank" rel="noreferrer" style={{ color: 'var(--muted)', textDecoration: 'none', fontSize: 12 }}>Open</a>
                        : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

// ── Small components ──────────────────────────────────────────────────────────
function Stat({ label, value, color }: { label: string; value: number; color?: string }) {
  return (
    <div>
      <p style={{ fontSize: 11, color: 'var(--muted)', marginBottom: 4, textTransform: 'uppercase', letterSpacing: '0.04em' }}>{label}</p>
      <p style={{ fontSize: 26, fontWeight: 700, color: color || 'var(--text)' }}>{value}</p>
    </div>
  );
}

function PriorityBadge({ p }: { p: string }) {
  return (
    <span className={`badge badge-${p.toLowerCase()}`}>{p}</span>
  );
}

function StatusBadge({ status }: { status: string }) {
  const map: Record<string, { color: string; label: string }> = {
    queued:  { color: 'var(--muted)', label: 'Queued' },
    running: { color: 'var(--accent)', label: 'Running' },
    done:    { color: 'var(--cold)',   label: 'Done' },
    error:   { color: 'var(--hot)',    label: 'Error' },
  };
  const s = map[status] || map.queued;
  return (
    <span style={{ fontSize: 12, color: s.color, fontWeight: 500 }}>
      {status === 'running' && (
        <span style={{
          display: 'inline-block', width: 6, height: 6, borderRadius: '50%',
          background: s.color, marginRight: 6,
          animation: 'pulse 1.5s ease-in-out infinite',
        }} />
      )}
      {s.label}
      <style>{`@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }`}</style>
    </span>
  );
}
