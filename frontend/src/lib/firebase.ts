import { initializeApp, getApps, type FirebaseApp } from "firebase/app";
import { getAuth, type Auth } from "firebase/auth";

const firebaseConfig = {
  apiKey: process.env.NEXT_PUBLIC_FIREBASE_API_KEY || "demo",
  authDomain: process.env.NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN || "demo.firebaseapp.com",
  projectId: process.env.NEXT_PUBLIC_FIREBASE_PROJECT_ID || "demo",
  appId: process.env.NEXT_PUBLIC_FIREBASE_APP_ID || "demo",
};

let app: FirebaseApp;
let auth: Auth;

export function getFirebaseApp(): FirebaseApp {
  if (!app) {
    app = getApps().length ? getApps()[0]! : initializeApp(firebaseConfig);
  }
  return app;
}

export function getFirebaseAuth(): Auth {
  if (!auth) {
    auth = getAuth(getFirebaseApp());
  }
  return auth;
}

/** True when Firebase is not configured — local/demo mode */
export function isDemoMode(): boolean {
  return (
    !process.env.NEXT_PUBLIC_FIREBASE_API_KEY ||
    process.env.NEXT_PUBLIC_FIREBASE_API_KEY === "demo"
  );
}
