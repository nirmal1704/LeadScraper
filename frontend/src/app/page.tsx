'use client';
import { useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { onAuthStateChanged } from 'firebase/auth';
import { auth } from '@/lib/firebase';

export default function Home() {
  const router = useRouter();
  useEffect(() => {
    const unsub = onAuthStateChanged(auth, (user) => {
      router.replace(user ? '/dashboard' : '/login');
    });
    return unsub;
  }, [router]);
  return (
    <div style={{ display: 'flex', height: '100vh', alignItems: 'center', justifyContent: 'center' }}>
      <div style={{ width: 20, height: 20, border: '2px solid #4f8ef7', borderTopColor: 'transparent', borderRadius: '50%', animation: 'spin 0.8s linear infinite' }} />
      <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
    </div>
  );
}
