import { File, FileBox, FileText, Film, Folder, Image } from 'lucide-react';

export type PrinterFileSort = 'name-asc' | 'name-desc' | 'size-asc' | 'size-desc' | 'date-asc' | 'date-desc';
export const PRINTER_FILE_ROW_CLASS = 'flex items-center gap-3 p-2 rounded-lg transition-colors';

export function getPrinterFileIcon(filename: string, directory: boolean) {
  if (directory) return Folder;
  switch (filename.toLowerCase().split('.').pop()) {
    case '3mf': return FileBox;
    case 'gcode': return FileText;
    case 'mp4': case 'avi': return Film;
    case 'png': case 'jpg': case 'jpeg': return Image;
    default: return File;
  }
}
