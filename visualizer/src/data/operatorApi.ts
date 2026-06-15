import { botApiUrl } from './arenaConfig';

export const OPERATOR_TOKEN_STORAGE_KEY = 'sandcastle.operatorToken';

export class OperatorAuthError extends Error {
  constructor(message = 'Enter the operator token in Match controls before using Bots or AI Lab.') {
    super(message);
    this.name = 'OperatorAuthError';
  }
}

export const getOperatorToken = () => {
  if (typeof window === 'undefined') {
    return '';
  }
  return window.sessionStorage.getItem(OPERATOR_TOKEN_STORAGE_KEY) || '';
};

export const setOperatorToken = (token: string) => {
  if (typeof window === 'undefined') {
    return;
  }
  if (token) {
    window.sessionStorage.setItem(OPERATOR_TOKEN_STORAGE_KEY, token);
  } else {
    window.sessionStorage.removeItem(OPERATOR_TOKEN_STORAGE_KEY);
  }
};

export const botApiRequest = (path: string, options: RequestInit = {}, token = getOperatorToken()) => {
  if (!token) {
    throw new OperatorAuthError();
  }
  const headers = new Headers(options.headers);
  headers.set('Authorization', `Bearer ${token}`);
  return fetch(`${botApiUrl}${path}`, { ...options, headers });
};
