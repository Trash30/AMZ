const fs = require('fs');
const path = require('path');
const dotenv = require('dotenv');
const { scrapePromoPage, scrapeProductPages, determineAvailability, closePersistentBrowser } = require('./scraper');
const { sendDiscordNotification } = require('./discord');

// Load environment variables
dotenv.config();

// Initialize PM2 metrics if running under PM2
let pm2Io = null;
let pm2Metrics = {};

try {
    pm2Io = require('@pm2/io');
    pm2Metrics = {
        totalProducts: pm2Io.metric({
            name: 'Total Products Tracked',
            type: 'gauge'
        }),
        inStock: pm2Io.metric({
            name: 'Products In Stock',
            type: 'gauge'
        }),
        outOfStock: pm2Io.metric({
            name: 'Products Out of Stock',
            type: 'gauge'
        }),
        lastScanDuration: pm2Io.metric({
            name: 'Last Scan Duration (ms)',
            type: 'gauge'
        }),
        totalScans: pm2Io.counter({
            name: 'Total Scans'
        }),
        scanErrors: pm2Io.counter({
            name: 'Scan Errors'
        }),
        restocks: pm2Io.counter({
            name: 'Restocks Detected'
        })
    };
} catch (err) {
    console.log('[PM2] PM2 metrics integration disabled (missing @pm2/io or not under PM2):', err.message);
}


const promoUrl = process.env.AMAZON_PROMO_URL || 'https://www.amazon.fr/promotion/psp/A26013IELTKPDW';
const scanInterval = parseInt(process.env.SCAN_INTERVAL_MS || '10000', 10);
const headless = process.env.HEADLESS !== 'false';
const dbPath = path.join(__dirname, 'database.json');
const cacheMs = parseInt(process.env.PRODUCT_PAGE_CACHE_MS || '0', 10);
const enableProductScrape = process.env.ENABLE_PRODUCT_PAGE_SCRAPE === 'true';
const batchSize = parseInt(process.env.PRODUCT_PAGE_BATCH_SIZE || '2', 10);
const browserRecycleInterval = parseInt(process.env.BROWSER_RECYCLE_INTERVAL || '150', 10);
let scanCount = 0;

console.log("==========================================");
console.log("   AMAZON PROMOTION DISCORD MONITOR BOT   ");
console.log("==========================================");
console.log(`URL de promotion : ${promoUrl}`);
console.log(`Intervalle de scan : ${scanInterval / 1000} secondes`);
console.log(`Mode sans tête (headless) : ${headless}`);
console.log(`Base de données locale : ${dbPath}`);
console.log(`Recyclage du navigateur : tous les ${browserRecycleInterval} scans`);
console.log(`Cache page produit (ms) : ${cacheMs} ms (${cacheMs > 0 ? (cacheMs / 1000) + 's' : 'désactivé'})`);
console.log(`Scan direct des pages ASIN (lourd/lent) : ${enableProductScrape ? 'activé' : 'désactivé (ultra-rapide ⚡)'}`);
if (enableProductScrape) {
    console.log(`Taille du lot rolling queue : ${batchSize} produits par scan`);
}
console.log("==========================================\n");

// Initialize local database
let database = {};
let isFirstScan = true; // Tracks if it's the first scan of this bot run
if (fs.existsSync(dbPath)) {
    try {
        database = JSON.parse(fs.readFileSync(dbPath, 'utf8'));
        console.log(`[DB] Base de données chargée. ${Object.keys(database).length} produits suivis.`);
        
        // Initialize PM2 gauges
        if (pm2Io) {
            const dbProducts = Object.values(database);
            const dbTotal = dbProducts.length;
            const dbInStock = dbProducts.filter(p => p.isAvailable).length;
            const dbOutOfStock = dbTotal - dbInStock;
            
            if (pm2Metrics.totalProducts) pm2Metrics.totalProducts.set(dbTotal);
            if (pm2Metrics.inStock) pm2Metrics.inStock.set(dbInStock);
            if (pm2Metrics.outOfStock) pm2Metrics.outOfStock.set(dbOutOfStock);
        }
    } catch (e) {
        console.error("[DB] Erreur lors de la lecture de database.json. Réinitialisation...", e);
        database = {};
    }
} else {
    console.log("[DB] Première exécution : création d'une nouvelle base de données.");
    fs.writeFileSync(dbPath, JSON.stringify({}, null, 2), 'utf8');
}

/**
 * Sends a system startup alert to Discord with a complete recap of the first scan.
 * @param {Array<Object>} products - Scraped products list
 */
async function sendSystemStartAlert(products) {
    const webhookUrl = process.env.DISCORD_WEBHOOK_URL;
    if (!webhookUrl) return;

    const inStock = products.filter(p => p.isAvailable);
    const outOfStock = products.filter(p => !p.isAvailable);

    let recapDescription = `Le bot est maintenant actif et surveille la promotion.\n\n🔗 **[Lien de la Promotion](${promoUrl})**\n\n`;

    recapDescription += `### 🟢 En Stock (${inStock.length})\n`;
    if (inStock.length === 0) {
        recapDescription += "*Aucun produit en stock actuellement.*\n";
    } else {
        inStock.forEach(p => {
            const price = p.price ? `\`${p.price}\`` : 'Prix non visible';
            recapDescription += `- **[${p.title}](${p.url})** - ${price}\n`;
        });
    }

    recapDescription += `\n### 🔴 Hors Stock (${outOfStock.length})\n`;
    if (outOfStock.length === 0) {
        recapDescription += "*Aucun produit hors stock.*\n";
    } else {
        outOfStock.forEach(p => {
            const price = p.price ? `\`${p.price}\`` : 'Prix non visible';
            // const reason = p.availabilityText ? ` (*${p.availabilityText}*)` : '';
            recapDescription += `- **[${p.title}](${p.url})** - ${price}\n`;
        });
    }

    // Embed description length limit is 4096. Ensure we truncate safely if too long
    if (recapDescription.length > 4000) {
        recapDescription = recapDescription.substring(0, 3970) + "\n\n*(Et d'autres produits... Tronqué pour respecter la limite Discord)*";
    }

    const payload = {
        embeds: [{
            title: '🚀 Bot Démarré - Récapitulatif du Premier Scan',
            description: recapDescription,
            color: 10181046, // Purple
            fields: [
                {
                    name: '📊 Total Produits',
                    value: `**${products.length}**`,
                    inline: true
                },
                {
                    name: '⏱️ Intervalle / Cache',
                    value: `Scan: **${scanInterval / 1000}s** / Cache: **${cacheMs > 0 ? (cacheMs / 1000) + 's' : 'Non'}**`,
                    inline: true
                }
            ],
            timestamp: new Date().toISOString()
        }]
    };

    try {
        const response = await fetch(webhookUrl, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        if (!response.ok) {
            const errText = await response.text();
            console.error(`[Discord] Error sending boot recap alert. Status: ${response.status}. Msg: ${errText}`);
        } else {
            console.log("[Discord] Startup notification sent with scan recap.");
        }
    } catch (e) {
        console.error("[Discord] Error sending startup alert:", e);
    }
}

/**
 * Scrapes, compares state, alerts on changes, and updates database.
 */
async function checkProducts() {
    const scanStartTime = Date.now();
    console.log(`\n[${new Date().toLocaleTimeString()}] Début du scan de la page...`);
    let currentProducts = [];

    try {
        // 1. Scrape promotion grid page
        currentProducts = await scrapePromoPage(promoUrl, headless);
    } catch (e) {
        console.error(`[Error] Échec du scan :`, e.message);
        if (pm2Metrics.scanErrors) pm2Metrics.scanErrors.inc();
        return; // Don't crash the loop
    }

    // Safety guard: if the grid scan returns 0 products, suspect a transient load/rendering failure
    if (!currentProducts || currentProducts.length === 0) {
        console.log("[⚠️ Warning] Le scan a retourné 0 produit. C'est probablement une erreur de chargement. Scan ignoré pour éviter les fausses suppressions.");
        if (pm2Metrics.scanErrors) pm2Metrics.scanErrors.inc();
        return;
    }

    const isFirstRun = isFirstScan;
    let newProductsCount = 0;
    let restockedProductsCount = 0;

    const updatedDatabase = { ...database };
    const activeAsins = new Set(currentProducts.map(p => p.asin));

    if (enableProductScrape) {
        if (isFirstRun) {
            // 1. Startup Scan: Scrape ALL products in parallel batches of 4
            console.log(`[Startup] Premier scan : Initialisation complète de la BDD pour les ${currentProducts.length} produits...`);
            const allAsins = currentProducts.map(p => p.asin);
            const STARTUP_BATCH_SIZE = 7;
            const directScrapeResults = [];

            for (let i = 0; i < allAsins.length; i += STARTUP_BATCH_SIZE) {
                const batch = allAsins.slice(i, i + STARTUP_BATCH_SIZE);
                console.log(`[Startup] Scan en lot des pages produits (${i + 1} à ${Math.min(i + STARTUP_BATCH_SIZE, allAsins.length)} sur ${allAsins.length})...`);
                try {
                    const results = await scrapeProductPages(batch, headless);
                    directScrapeResults.push(...results);
                } catch (err) {
                    console.error(`[Startup Error] Échec du lot de pages produits :`, err.message);
                }
            }

            // Merge scraped details back to products
            const resultsMap = new Map(directScrapeResults.map(r => [r.asin, r]));
            for (const product of currentProducts) {
                const scraped = resultsMap.get(product.asin);
                if (scraped && !scraped.error) {
                    if (scraped.deliveryText) product.deliveryText = scraped.deliveryText;
                    if (scraped.availabilityText) product.availabilityText = scraped.availabilityText;
                    if (scraped.imageUrl) product.imageUrl = scraped.imageUrl;
                    product.lastProductPageScrape = new Date().toISOString();
                } else {
                    // Carry over from DB if direct scrape failed but we have historical data
                    const prevProduct = database[product.asin];
                    if (prevProduct) {
                        product.deliveryText = prevProduct.deliveryText;
                        product.availabilityText = prevProduct.availabilityText;
                        if (prevProduct.imageUrl) product.imageUrl = prevProduct.imageUrl;
                        product.lastProductPageScrape = prevProduct.lastProductPageScrape;
                    }
                }

                // Evaluate availability
                product.isAvailable = determineAvailability(product.deliveryText, product.availabilityText);
            }
        } else {
            // 2. Subsequent Scan (Staggered Rolling Queue): Sort by lastProductPageScrape and scrape batchSize products
            const sortedProducts = [...currentProducts].sort((a, b) => {
                const timeA = database[a.asin] && database[a.asin].lastProductPageScrape ? new Date(database[a.asin].lastProductPageScrape).getTime() : 0;
                const timeB = database[b.asin] && database[b.asin].lastProductPageScrape ? new Date(database[b.asin].lastProductPageScrape).getTime() : 0;
                return timeA - timeB;
            });

            const selectedProducts = sortedProducts.slice(0, batchSize);
            const asinsToScrape = selectedProducts.map(p => p.asin);
            const selectedAsinsSet = new Set(asinsToScrape);

            console.log(`[Scan] Rolling Queue : Sélection de ${asinsToScrape.length} produit(s) plus ancien(s) à rafraîchir...`);
            selectedProducts.forEach(p => {
                const prev = database[p.asin];
                const lastTime = prev && prev.lastProductPageScrape ? new Date(prev.lastProductPageScrape).toLocaleTimeString() : 'jamais';
                console.log(`  - ASIN ${p.asin} (${p.title}) [Dernier refresh: ${lastTime}]`);
            });

            let directScrapeResults = [];
            if (asinsToScrape.length > 0) {
                try {
                    const scrapeStartTime = Date.now();
                    directScrapeResults = await scrapeProductPages(asinsToScrape, headless);
                    console.log(`[Scan] Scanné ${asinsToScrape.length} page(s) en ${((Date.now() - scrapeStartTime) / 1000).toFixed(2)}s`);
                } catch (err) {
                    console.error("[Error] Échec du scan rolling queue :", err.message);
                }
            }

            const resultsMap = new Map(directScrapeResults.map(r => [r.asin, r]));

            for (const product of currentProducts) {
                if (selectedAsinsSet.has(product.asin)) {
                    // Merging fresh scrape results
                    const scraped = resultsMap.get(product.asin);
                    if (scraped && !scraped.error) {
                        if (scraped.deliveryText) product.deliveryText = scraped.deliveryText;
                        if (scraped.availabilityText) product.availabilityText = scraped.availabilityText;
                        if (scraped.imageUrl) product.imageUrl = scraped.imageUrl;
                        product.lastProductPageScrape = new Date().toISOString();
                    } else {
                        // Fallback to database if scrape failed
                        const prevProduct = database[product.asin];
                        if (prevProduct) {
                            product.deliveryText = prevProduct.deliveryText;
                            product.availabilityText = prevProduct.availabilityText;
                            if (prevProduct.imageUrl) product.imageUrl = prevProduct.imageUrl;
                            product.lastProductPageScrape = prevProduct.lastProductPageScrape;
                        }
                    }
                    product.isAvailable = determineAvailability(product.deliveryText, product.availabilityText);
                } else {
                    // Carry over from DB since it was not selected this round
                    const prevProduct = database[product.asin];
                    if (prevProduct) {
                        product.deliveryText = prevProduct.deliveryText;
                        product.availabilityText = prevProduct.availabilityText;
                        if (prevProduct.imageUrl) product.imageUrl = prevProduct.imageUrl;
                        product.isAvailable = prevProduct.isAvailable;
                        product.lastProductPageScrape = prevProduct.lastProductPageScrape;
                    } else {
                        // Brand new product, but couldn't fit in this batch.
                        product.isAvailable = false;
                    }
                }
            }
        }
    } else {
        // Direct scraping disabled: strictly rely on grid information
        console.log(`[Scan] Grid has ${currentProducts.length} items. Product page scraping is disabled.`);
        for (const product of currentProducts) {
            product.isAvailable = determineAvailability(product.deliveryText, product.availabilityText);
        }
    }

    // 3. Process database updates & transition notifications
    for (const product of currentProducts) {
        const prevProduct = database[product.asin];

        if (!prevProduct) {
            // New product detected (never seen before)
            newProductsCount++;
            console.log(`[🆕 Nouveau] ASIN ${product.asin} : ${product.title} (${product.price || 'Gratuit/Promotion'}) - Stock: ${product.isAvailable ? '🟢 EN STOCK' : '🔴 HORS STOCK'}`);

            // Save to updated database
            updatedDatabase[product.asin] = {
                ...product,
                lastSeen: new Date().toISOString()
            };
            delete updatedDatabase[product.asin].missingCount; // Ensure missingCount is reset if it existed in another form

            // Only notify on Discord if it is not the very first execution of the bot
            if (!isFirstRun) {
                await sendDiscordNotification({
                    type: 'new',
                    product: product
                });
                await new Promise(r => setTimeout(r, 100));
            }
        } else {
            // Existing product - check transition from Out-of-Stock to In-Stock
            const transitionedToInStock = !prevProduct.isAvailable && product.isAvailable;

            if (transitionedToInStock) {
                restockedProductsCount++;
                console.log(`[🟢 Retour en stock] ASIN ${product.asin} : ${product.title} est de nouveau disponible !`);

                if (pm2Metrics.restocks) pm2Metrics.restocks.inc();

                if (!isFirstRun) {
                    await sendDiscordNotification({
                        type: 'restock',
                        product: product,
                        previousAvailability: prevProduct.availabilityText,
                        previousDelivery: prevProduct.deliveryText
                    });
                    await new Promise(r => setTimeout(r, 800));
                }
            }

            // Update record in database
            updatedDatabase[product.asin] = {
                ...prevProduct,
                ...product, // Overwrite with fresh values
                lastSeen: new Date().toISOString()
            };
            delete updatedDatabase[product.asin].missingCount; // Reset missing count since it is present
        }
    }

    // If products are removed from the promo page, delete them from the database after a grace period of 3 consecutive scans
    const GRACE_PERIOD_SCANS = 5;
    for (const asin of Object.keys(database)) {
        if (!activeAsins.has(asin)) {
            const product = database[asin];
            const currentMissingCount = (product.missingCount || 0) + 1;

            if (currentMissingCount >= GRACE_PERIOD_SCANS) {
                console.log(`[🗑️ Supprimé] ASIN ${asin} : ${product.title} a été absent pendant ${GRACE_PERIOD_SCANS} scans consécutifs. Supprimé de la BDD.`);
                delete updatedDatabase[asin];
            } else {
                console.log(`[⚠️ Absent] ASIN ${asin} : ${product.title} absent du scan. Essai ${currentMissingCount}/${GRACE_PERIOD_SCANS} avant suppression.`);
                updatedDatabase[asin] = {
                    ...product,
                    missingCount: currentMissingCount
                };
            }
        }
    }

    // Save changes to disk
    database = updatedDatabase;
    fs.writeFileSync(dbPath, JSON.stringify(database, null, 2), 'utf8');
    console.log(`[DB] database.json mise à jour. Total produits suivis : ${Object.keys(database).length}`);

    // Update PM2 metrics
    if (pm2Io) {
        const dbProducts = Object.values(database);
        const dbTotal = dbProducts.length;
        const dbInStock = dbProducts.filter(p => p.isAvailable).length;
        const dbOutOfStock = dbTotal - dbInStock;
        const duration = Date.now() - scanStartTime;

        if (pm2Metrics.totalProducts) pm2Metrics.totalProducts.set(dbTotal);
        if (pm2Metrics.inStock) pm2Metrics.inStock.set(dbInStock);
        if (pm2Metrics.outOfStock) pm2Metrics.outOfStock.set(dbOutOfStock);
        if (pm2Metrics.lastScanDuration) pm2Metrics.lastScanDuration.set(duration);
        if (pm2Metrics.totalScans) pm2Metrics.totalScans.inc();
    }

    // If first run, send a startup alert
    if (isFirstRun) {
        console.log(`[System] Initialisation réussie. Suivi de ${currentProducts.length} produits de départ.`);
        await sendSystemStartAlert(currentProducts);
        isFirstScan = false;
    } else {
        console.log(`[Scan] Terminé. ${newProductsCount} nouveaux, ${restockedProductsCount} retour(s) en stock.`);
    }

    // Recycle browser periodically to prevent memory leaks
    scanCount++;
    if (scanCount % browserRecycleInterval === 0) {
        console.log(`[System] Recyclage périodique du navigateur pour libérer la mémoire (Scan #${scanCount})...`);
        try {
            await closePersistentBrowser();
        } catch (recycleErr) {
            console.error("[System] Erreur lors du recyclage du navigateur :", recycleErr.message);
        }
    }
}

// Main execution loop
let isRunning = true;

async function mainLoop() {
    if (!isRunning) return;

    try {
        await checkProducts();
    } catch (e) {
        console.error("[Main] Critical error in check loop:", e);
    }

    // Schedule next run
    if (isRunning) {
        setTimeout(mainLoop, scanInterval);
    }
}

// Graceful shutdown handling
async function shutdown() {
    console.log("\n[System] Arrêt du bot demandé...");
    isRunning = false;
    await closePersistentBrowser();
    process.exit(0);
}

process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);

// Start the bot
mainLoop();
