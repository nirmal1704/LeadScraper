const BASE = process.env.NEXT_PUBLIC_BACKEND_URL || 'http://localhost:8000';

async function headers(token: string) {
  return { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` };
}

export async function startJob(token: string, query: string, sources: string[]) {
  const res = await fetch(`${BASE}/jobs`, {
    method: 'POST',
    headers: await headers(token),
    body: JSON.stringify({ query, sources }),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json() as Promise<{ job_id: string }>;
}

export async function stopJob(token: string, jobId: string) {
  await fetch(`${BASE}/jobs/${jobId}`, {
    method: 'DELETE',
    headers: await headers(token),
  });
}

export async function downloadExcel(token: string, jobId: string) {
  const res = await fetch(`${BASE}/jobs/${jobId}/download`, {
    method: 'GET',
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!res.ok) throw new Error(await res.text());
  
  const blob = await res.blob();
  const url = window.URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.style.display = 'none';
  a.href = url;
  a.download = `leads_${jobId.substring(0, 8)}.xlsx`;
  document.body.appendChild(a);
  a.click();
  window.URL.revokeObjectURL(url);
}
