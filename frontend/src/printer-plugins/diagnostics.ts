import type { PrinterDiagnosticResult } from '../api/client';

/** Keep legacy warning-only saves unchanged, while presenting plugin advice. */
export function shouldReviewDiagnostic(result: PrinterDiagnosticResult): boolean {
  return result.overall === 'problems' || result.checks.some(check => check.status === 'fail'
    || check.status === 'warn' && !!(check.title || check.message || check.hint));
}
