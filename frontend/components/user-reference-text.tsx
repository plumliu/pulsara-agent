import { splitUserFileReferences } from '../lib/file-reference';
import { FileReferenceChip } from './file-reference-chip';
/** Sent text is read from its original markers; current skill inventory cannot rewrite history. */
export function UserReferenceText({ text }: { text: string }) {
  return <>{splitUserFileReferences(text).map((part,index)=>typeof part==='string'?part:<FileReferenceChip key={index} reference={part}/>)}</>;
}
