import React, { createContext, useContext, useState, useCallback } from 'react';
import type { OtpChallenge, User } from '../types';
import { authAPI, setAuthToken } from '../services/api';
import { PORTAL, PORTAL_NAME, ROLE_PORTAL_URL, roleAllowedHere } from '../utils/portal';

interface AuthContextType {
  user: User | null;
  token: string | null;
  login: (username: string, password: string) => Promise<OtpChallenge>;
  verifyOtp: (otpToken: string, code: string) => Promise<void>;
  resendOtp: (otpToken: string) => Promise<OtpChallenge>;
  logout: () => void;
  updateUser: (patch: Partial<User>) => void;
  isLoading: boolean;
}

const AuthContext = createContext<AuthContextType | null>(null);

export const AuthProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [user, setUser] = useState<User | null>(() => {
    const stored = localStorage.getItem('clari5pay_user');
    if (!stored) return null;
    const u = JSON.parse(stored) as User;
    // Drop a stale session that doesn't belong on this portal (e.g. opened a different one).
    return roleAllowedHere(u.role) ? u : null;
  });
  const [token, setToken] = useState<string | null>(() =>
    localStorage.getItem('clari5pay_token')
  );
  const [isLoading, setIsLoading] = useState(false);

  const applySession = useCallback((accessToken: string, u: User) => {
    // Each role-portal only admits its own role; send others to the right portal.
    if (!roleAllowedHere(u.role)) {
      const url = ROLE_PORTAL_URL[u.role];
      throw new Error(`This is the ${PORTAL_NAME[PORTAL]}. Your account signs in at ${url || 'the correct portal'}.`);
    }
    localStorage.setItem('clari5pay_user', JSON.stringify(u));
    localStorage.setItem('clari5pay_token', accessToken);
    setAuthToken(accessToken);
    setUser(u);
    setToken(accessToken);
  }, []);

  // Step 1: validate credentials. When OTP is ON → returns a challenge (no session yet).
  // When OTP is OFF → backend returns a token directly, so we establish the session here.
  const login = useCallback(async (username: string, password: string): Promise<OtpChallenge> => {
    setIsLoading(true);
    try {
      const res = await authAPI.login({ username, password });
      if ('access_token' in res) {
        applySession(res.access_token, res.user);
        return { otpRequired: false, otpToken: '', email: '' };
      }
      return res;
    } finally {
      setIsLoading(false);
    }
  }, [applySession]);

  // Step 2: verify the OTP → establish the session (token attached before the dashboard mounts).
  const verifyOtp = useCallback(async (otpToken: string, code: string) => {
    setIsLoading(true);
    try {
      const res = await authAPI.verifyOtp(otpToken, code);
      applySession(res.access_token, res.user);
    } finally {
      setIsLoading(false);
    }
  }, [applySession]);

  const resendOtp = useCallback((otpToken: string) => authAPI.resendOtp(otpToken), []);

  const logout = useCallback(() => {
    setUser(null);
    setToken(null);
    setAuthToken(null);
    localStorage.removeItem('clari5pay_user');
    localStorage.removeItem('clari5pay_token');
  }, []);

  const updateUser = useCallback((patch: Partial<User>) => {
    setUser((prev) => {
      if (!prev) return prev;
      const next = { ...prev, ...patch };
      localStorage.setItem('clari5pay_user', JSON.stringify(next));
      return next;
    });
  }, []);

  return (
    <AuthContext.Provider value={{ user, token, login, verifyOtp, resendOtp, logout, updateUser, isLoading }}>
      {children}
    </AuthContext.Provider>
  );
};

export const useAuth = () => {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used within AuthProvider');
  return ctx;
};
