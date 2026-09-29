import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { api, setCsrf } from "./api";

export type Me = { username: string; role: string; permissions: string[]; csrf_token: string };
type Ctx = {
  me: Me | null;
  loading: boolean;
  login: (u: string, p: string) => Promise<void>;
  logout: () => Promise<void>;
  can: (perm: string) => boolean;
};

const AuthContext = createContext<Ctx>(null!);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<Me | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const m = await api.get<Me>("/auth/me");
      setCsrf(m.csrf_token);
      setMe(m);
    } catch {
      setMe(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const login = async (username: string, password: string) => {
    const r = await api.post<{ csrf_token: string }>("/auth/login", { username, password });
    setCsrf(r.csrf_token);
    await refresh();
  };
  const logout = async () => {
    await api.post("/auth/logout");
    setMe(null);
  };
  const can = (perm: string) => !!me?.permissions.includes(perm);
  return <AuthContext.Provider value={{ me, loading, login, logout, can }}>{children}</AuthContext.Provider>;
}

export const useAuth = () => useContext(AuthContext);
