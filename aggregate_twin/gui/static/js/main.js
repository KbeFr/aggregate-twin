import { boot } from './core/shell.js';
import { registerWorldSheet } from './world/world-sheet.js';
import { registerNetworkSheet } from './network/network-sheet.js';
import { initAutocomplete } from './discovery/autocomplete.js';
import { initReview } from './discovery/review-panel.js';

registerWorldSheet();
registerNetworkSheet();
initAutocomplete();
initReview();
boot('world');
