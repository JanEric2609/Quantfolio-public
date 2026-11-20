import { useHotkeys } from "react-hotkeys-hook";
import { useEffect } from "react";
import { create } from "zustand";

export interface Command { id: string; label: string; group: string; run: () => void; }
interface CommandStore { commands: Command[]; register(c: Command): void; unregister(id: string): void; }
export const useCommandStore = create<CommandStore>((set) => ({
  commands: [],
  register(c) { set((s) => ({ commands: [...s.commands.filter((x) => x.id !== c.id), c] })); },
  unregister(id) { set((s) => ({ commands: s.commands.filter((c) => c.id !== id) })); },
}));

export function useRegisterCommand(c: Command) {
  const register = useCommandStore((s) => s.register);
  const unregister = useCommandStore((s) => s.unregister);
  // register in useEffect to avoid memory leak and properly unregister on unmount
  useEffect(() => {
    register(c);
    return () => unregister(c.id);
  }, [c.id, register, unregister, c]);
}

export function useGlobalHotkeys(openPalette: () => void) {
  useHotkeys("mod+k", (e) => { e.preventDefault(); openPalette(); }, { enableOnFormTags: true });
}
