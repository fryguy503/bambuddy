import type { ReactNode } from 'react';
import { ArrowUpDown, ChevronLeft, HardDrive, RefreshCw, Search, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { PrinterFileSort } from '../utils/printerFiles';
import { Button } from './Button';

export function PrinterFileBrowserHeader({ printerName, onClose, children }: {
  printerName: string;
  onClose: () => void;
  children?: ReactNode;
}) {
  const { t } = useTranslation();
  return (
    <div className="flex items-center justify-between p-4 border-b border-bambu-dark-tertiary flex-shrink-0">
      <div className="flex items-center gap-3 min-w-0">
        <HardDrive className="w-5 h-5 text-bambu-green shrink-0" />
        <div className="min-w-0">
          <h2 className="text-lg font-semibold text-white">{t('printerFiles.title')}</h2>
          <p className="text-sm text-bambu-gray truncate">{printerName}</p>
        </div>
      </div>
      <div className="flex items-center gap-4">
        {children}
        <button
          type="button"
          onClick={onClose}
          className="text-bambu-gray hover:text-white transition-colors"
          title={t('fileManagerModal.closeFileManager')}
          aria-label={t('fileManagerModal.closeFileManager')}
        >
          <X className="w-5 h-5" />
        </button>
      </div>
    </div>
  );
}

export function PrinterFileBrowserToolbar({
  path, shortcuts, onNavigate, search, onSearch, sort, onSort, onRefresh, refreshing, dateSort = true,
}: {
  path: string;
  shortcuts: { path: string; label: string }[];
  onNavigate: (path: string) => void;
  search: string;
  onSearch: (value: string) => void;
  sort: PrinterFileSort;
  onSort: (value: PrinterFileSort) => void;
  onRefresh: () => void;
  refreshing: boolean;
  dateSort?: boolean;
}) {
  const { t } = useTranslation();
  const options: { value: PrinterFileSort; label: string }[] = [
    { value: 'name-asc', label: `${t('archives.sortName')} (A-Z)` },
    { value: 'name-desc', label: `${t('archives.sortName')} (Z-A)` },
    { value: 'size-asc', label: `${t('archives.sortSize')} (${t('archives.sortSmallest')})` },
    { value: 'size-desc', label: `${t('archives.sortSize')} (${t('archives.sortLargest')})` },
    ...(dateSort ? [
      { value: 'date-asc' as const, label: t('archives.sortOldest') },
      { value: 'date-desc' as const, label: t('archives.sortNewest') },
    ] : []),
  ];
  return (
    <div className="flex flex-wrap items-center gap-2 p-3 border-b border-bambu-dark-tertiary bg-bambu-dark/50 flex-shrink-0">
      {shortcuts.map(dir => (
        <button
          type="button"
          key={dir.path}
          onClick={() => { onNavigate(dir.path); onSearch(''); }}
          className={`px-3 py-1 text-sm rounded-full transition-colors ${path === dir.path
            ? 'bg-bambu-green text-white'
            : 'bg-bambu-dark-tertiary text-bambu-gray hover:text-white'}`}
        >
          {dir.label}
        </button>
      ))}
      <div className="flex-1" />
      <div className="relative">
        <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-4 h-4 text-bambu-gray" />
        <input
          type="text"
          aria-label={t('printerFiles.filterPlaceholder')}
          placeholder={t('printerFiles.filterPlaceholder')}
          value={search}
          onChange={event => onSearch(event.target.value)}
          className="w-40 pl-8 pr-3 py-1.5 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-sm focus:border-bambu-green focus:outline-none"
        />
      </div>
      <div className="relative flex items-center gap-1">
        <ArrowUpDown className="w-4 h-4 text-bambu-gray" />
        <select
          value={sort}
          onChange={event => onSort(event.target.value as PrinterFileSort)}
          className="appearance-none bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-sm py-1.5 pl-2 pr-6 focus:border-bambu-green focus:outline-none cursor-pointer"
          title={t('fileManagerModal.sortFiles')}
          aria-label={t('fileManagerModal.sortFiles')}
        >
          {options.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}
        </select>
      </div>
      <Button
        variant="secondary"
        size="sm"
        onClick={onRefresh}
        disabled={refreshing}
        aria-label={t('common.refresh')}
        title={t('common.refresh')}
      >
        <RefreshCw className={`w-4 h-4 ${refreshing ? 'animate-spin' : ''}`} />
      </Button>
    </div>
  );
}

export function PrinterFileBrowserPath({ path, onUp }: { path: string; onUp: () => void }) {
  const { t } = useTranslation();
  return (
    <div className="flex items-center gap-2 px-4 py-2 bg-bambu-dark text-sm flex-shrink-0">
      <button
        type="button"
        onClick={onUp}
        disabled={path === '/'}
        className="p-1 rounded hover:bg-bambu-dark-tertiary disabled:opacity-50 disabled:cursor-not-allowed"
        title={t('fileManagerModal.goToParentFolder')}
        aria-label={t('fileManagerModal.goToParentFolder')}
      >
        <ChevronLeft className="w-4 h-4" />
      </button>
      <span className="text-bambu-gray font-mono truncate" title={path}>{path}</span>
    </div>
  );
}

export function PrinterFileBrowserFooter({ children }: { children: ReactNode }) {
  return (
    <div className="flex flex-wrap gap-3 items-center justify-between p-4 border-t border-bambu-dark-tertiary bg-bambu-dark/50 flex-shrink-0">
      {children}
    </div>
  );
}
