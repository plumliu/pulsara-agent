export const OAUTH_RETURN_URL = 'pulsara://oauth/return';

// This link only focuses the app. OAuth codes and grant state never come from it.
export function isOAuthReturn(value: string): boolean {
  return value === OAUTH_RETURN_URL;
}
