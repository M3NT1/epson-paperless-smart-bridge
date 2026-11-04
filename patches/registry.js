import { WF3620_ENTRY } from "./wf3620.js";
import { XP620_ENTRY } from "./xp620.js";
import { ET2550_ENTRY } from "./et2550.js";

// Epson Expression Premium XP-610 (PID 08B6) shares the exact same ESC/I fixed-flatbed
// dialect as the XP-620 (PID 08C8).
const XP610_ENTRY = {
    ...XP620_ENTRY,
    name: "xp610",
    productName: "PID 08B6"
};

// Every entry with a non-null productName gets a PID-lookup slot.
const PID_ENTRIES = [XP620_ENTRY, XP610_ENTRY, ET2550_ENTRY];
const BY_PID = {};
for (const entry of PID_ENTRIES) {
    if (entry.productName === null) {
        throw new Error(`registry: dialect "${entry.name}" has no productName — cannot key BY_PID`);
    }
    BY_PID[entry.productName] = entry;
}

// PIDs with a dedicated legacy dialect.
export const LEGACY_DIALECT_PIDS = new Set(Object.keys(BY_PID));

export function resolveLegacyEntry(productName) {
    return (productName && BY_PID[productName]) || WF3620_ENTRY;
}
