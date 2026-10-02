"use client";

import { useEffect, useState } from "react";
import { apiFetch } from "./auth";
import styles from "./ResourceQueueStatus.module.css";

type QueueTicket = {
  label: string;
  position: number;
  status: "queued" | "running";
  remaining: number;
};

type QueueState = {
  active: { label: string } | null;
  waiting: number;
  tickets: QueueTicket[];
};

export default function ResourceQueueStatus() {
  const [queue, setQueue] = useState<QueueState | null>(null);

  useEffect(() => {
    let stopped = false;
    const refresh = async () => {
      try {
        const response = await apiFetch("/api/resource-queue/status", { cache: "no-store" });
        if (!response.ok) return;
        const data = (await response.json()) as QueueState;
        if (!stopped) setQueue(data);
      } catch {
        // Retry on the next poll; queue status must not block the page.
      }
    };

    void refresh();
    const timer = window.setInterval(() => void refresh(), 3000);
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, []);

  if (!queue || (queue.waiting === 0 && queue.tickets.length === 0)) return null;
  const waiting = queue.tickets.filter((ticket) => ticket.status === "queued");

  return (
    <aside className={styles.bar} aria-live="polite">
      <span className={styles.pulse} aria-hidden="true" />
      <span className={styles.active}>
        {queue.active ? `Sedang berjalan: ${queue.active.label}` : "Slot server siap"}
      </span>
      {waiting.map((ticket, index) => (
        <span className={styles.ticket} key={`${ticket.label}-${index}`}>
          {ticket.label}: antrean ke-{ticket.position}
        </span>
      ))}
      {queue.tickets.some((ticket) => ticket.status === "running") && (
        <span className={styles.ticket}>Pekerjaanmu sedang berjalan</span>
      )}
      <span className={styles.total}>{queue.waiting} batch</span>
    </aside>
  );
}