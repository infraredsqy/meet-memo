import { fetchSessions } from '../api/sessions';
import { initialSelection } from './model';
import type { SessionsResponse } from '../contract';

export function loadSessionList(io: {
  onList: (data: SessionsResponse) => void;
  onSelection: (seat: (current: string | null) => string | null) => void;
  onFail?: (error: unknown) => void;
}): Promise<SessionsResponse | null> {
  return fetchSessions().then(
    (data) => {
      io.onList(data);
      io.onSelection((current) => current ?? initialSelection(data.sessions, data.recent));
      return data;
    },
    (error) => {
      io.onFail?.(error);
      return null;
    },
);
}
