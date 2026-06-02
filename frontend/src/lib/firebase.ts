import { initializeApp, getApps, FirebaseApp } from 'firebase/app';
import { Auth, getAuth, GoogleAuthProvider } from 'firebase/auth';
import { Firestore, getFirestore } from 'firebase/firestore';

const firebaseConfig = {
  apiKey:            process.env.NEXT_PUBLIC_FIREBASE_API_KEY,
  authDomain:        process.env.NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN,
  projectId:         process.env.NEXT_PUBLIC_FIREBASE_PROJECT_ID,
  messagingSenderId: process.env.NEXT_PUBLIC_FIREBASE_MESSAGING_SENDER_ID,
  appId:             process.env.NEXT_PUBLIC_FIREBASE_APP_ID,
};

// Only initialise on the client side — never during SSR/build
let app: FirebaseApp | undefined;
let _auth: ReturnType<typeof getAuth> | undefined;
let _db: ReturnType<typeof getFirestore> | undefined;
let _googleProvider: GoogleAuthProvider | undefined;

function getApp(): FirebaseApp {
  if (!app) {
    app = getApps().length === 0 ? initializeApp(firebaseConfig) : getApps()[0];
  }
  return app;
}

export function getFirebaseAuth() {
  if (!_auth) _auth = getAuth(getApp());
  return _auth;
}

export function getFirebaseDb() {
  if (!_db) _db = getFirestore(getApp());
  return _db;
}

export function getGoogleProvider() {
  if (!_googleProvider) _googleProvider = new GoogleAuthProvider();
  return _googleProvider;
}

// Convenience re-exports for components that are always client-side
export const auth: Auth = typeof window !== 'undefined' ? getFirebaseAuth() : (null as unknown as Auth);
export const db: Firestore = typeof window !== 'undefined' ? getFirebaseDb() : (null as unknown as Firestore);
export const googleProvider: GoogleAuthProvider =
  typeof window !== 'undefined' ? getGoogleProvider() : (null as unknown as GoogleAuthProvider);
