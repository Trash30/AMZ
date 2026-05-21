const { chromium } = require('playwright');
const dotenv = require('dotenv');
dotenv.config();

const timeoutMs = parseInt(process.env.TIMEOUT_MS || '30000', 10);

/**
 * Clean out boilerplate from delivery text and check if it contains a specific delivery date or date range.
 * @param {string} deliveryText - Delivery message
 * @param {string} availabilityText - Availability message
 * @returns {boolean} True if it represents a valid stock delivery date
 */
function determineAvailability(deliveryText, availabilityText) {
    const textLower = (deliveryText || '').toLowerCase().trim();
    const availLower = (availabilityText || '').toLowerCase().trim();
    
    // Explicitly out of stock indications
    if (availLower.includes('indisponible') || availLower.includes('rupture de stock')) {
        return false;
    }
    
    // Clean out standard boilerplate texts
    let cleanedText = textLower
        .replace(/livraison gratuite pour les membres prime/g, '')
        .replace(/livraison gratuite/g, '')
        .replace(/pour les membres prime/g, '')
        .replace(/livraison standard/g, '')
        .replace(/livraison/g, '')
        .replace(/gratuite/g, '')
        .trim();
        
    // Strip shipping prices (e.g. "4,99 €", "5,15€", "5 €") to prevent them from triggering hasDigit
    cleanedText = cleanedText.replace(/\d+([,.]\d+)?\s*€/g, '').trim();
        
    // Expected date markers (months or day names)
    const monthsRegex = /(janv|févr|mar|avr|mai|juin|juil|août|sept|oct|nov|déc)/i;
    const daysRegex = /(lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche|demain)/i;
    
    const hasDate = monthsRegex.test(cleanedText) || daysRegex.test(cleanedText);
    
    // If we have an explicit delivery date range/day, it is orderable and thus "in stock"
    if (hasDate) {
        return true;
    }
    
    // Fallback: Check if the availability status explicitly says it is in stock/available
    if (availLower.includes('en stock') || availLower.includes('disponible')) {
        // But make sure it doesn't have a long-term delay like "habituellement expédié sous"
        if (!availLower.includes('habituellement') && !availLower.includes('expédié sous')) {
            return true;
        }
    }
    
    return false;
}

let persistentBrowser = null;
let persistentContext = null;

/**
 * Initializes or retrieves the persistent Playwright context.
 */
async function getPersistentContext(headless = true) {
    if (persistentContext) {
        return persistentContext;
    }

    console.log("[Scraper] Launching persistent Chromium instance...");
    const launchOptions = {
        headless: headless,
        args: [
            '--no-sandbox',
            '--disable-setuid-sandbox',
            '--disable-blink-features=AutomationControlled'
        ]
    };
    if (process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH) {
        launchOptions.executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH;
    }
    persistentBrowser = await chromium.launch(launchOptions);

    persistentContext = await persistentBrowser.newContext({
        userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        viewport: { width: 1280, height: 800 },
        locale: 'fr-FR',
        extraHTTPHeaders: {
            'Accept-Language': 'fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7',
            'Cache-Control': 'no-cache, no-store, must-revalidate',
            'Pragma': 'no-cache',
            'Expires': '0'
        }
    });

    return persistentContext;
}

/**
 * Closes the persistent browser if open.
 */
async function closePersistentBrowser() {
    if (persistentBrowser) {
        console.log("[Scraper] Closing persistent browser...");
        await persistentBrowser.close().catch(() => { });
        persistentBrowser = null;
        persistentContext = null;
    }
}

/**
 * Scrapes the Amazon promotion page, accepting the cookie banner, loading all items 
 * by clicking "Afficher plus" recursively, and extracting all product details.
 * @param {string} url - The Amazon promotion URL
 * @param {boolean} headless - Whether to run the browser in headless mode
 * @returns {Promise<Array<Object>>} List of scraped products
 */
async function scrapePromoPage(url, headless = true) {
    const context = await getPersistentContext(headless);
    const page = await context.newPage();

    try {
        // Disable caching completely at the browser/network level for this page
        try {
            const client = await page.context().newCDPSession(page);
            await client.send('Network.setCacheDisabled', { cacheDisabled: true });
        } catch (e) {
            console.log("[Scraper] Warning: Could not disable cache via CDP on promo page:", e.message);
        }

        // Optimize speed and bandwidth by blocking stylesheets, fonts, media, and tracking scripts
        await page.route('**/*', (route) => {
            const req = route.request();
            const type = req.resourceType();
            const reqUrl = req.url();

            if (
                type === 'font' ||
                type === 'media' ||
                reqUrl.includes('google-analytics') ||
                reqUrl.includes('analytics') ||
                reqUrl.includes('amazon-adsystem') ||
                reqUrl.includes('doubleclick') ||
                reqUrl.includes('device-metrics')
            ) {
                route.abort();
            } else {
                route.continue();
            }
        });

        const cacheBypassUrl = `${url}${url.includes('?') ? '&' : '?'}cb=${Date.now()}`;
        console.log(`[Scraper] Navigating to: ${cacheBypassUrl}`);
        await page.goto(cacheBypassUrl, { waitUntil: 'domcontentloaded', timeout: timeoutMs });

        // Accept the cookies consent banner immediately if visible
        try {
            const cookieAcceptBtn = page.locator('#sp-cc-accept');
            if (await cookieAcceptBtn.count() > 0) {
                console.log("[Scraper] Cookie consent banner detected. Accepting...");
                await cookieAcceptBtn.click();
                await page.waitForTimeout(500);
            }
        } catch (e) {
            console.log("[Scraper] Cookie acceptance check bypassed:", e.message);
        }

        // Wait for dynamic React content to finish hydration
        console.log(`[Scraper] Waiting for product grid to hydrate...`);
        try {
            await page.waitForFunction(() => {
                const titleEl = document.querySelector('[data-name="productTitle"]');
                return titleEl && titleEl.textContent.trim().length > 5;
            }, { timeout: 10000 });
            await page.waitForTimeout(300);
        } catch (e) {
            const title = await page.title();
            if (title.includes('Robot Check') || title.includes('CAPTCHA')) {
                await closePersistentBrowser();
                throw new Error("Amazon block: Captcha page detected.");
            }
            console.log("[Scraper] Warning: Product hydration timed out. Proceeding anyway...");
        }

        // Loop to click the "Afficher plus" button until all pages of products are loaded
        console.log(`[Scraper] Loading all products via 'Afficher plus'...`);
        let clickCount = 0;
        while (true) {
            const showMoreBtn = page.locator('#showMore');
            if (await showMoreBtn.count() === 0) {
                break;
            }

            // Scroll to the container first
            await page.evaluate(() => {
                const container = document.getElementById('showMoreBtnContainer');
                if (container) {
                    container.scrollIntoView({ behavior: 'auto', block: 'center' });
                }
            });
            await page.waitForTimeout(600);

            // Double check visibility
            const isVisible = await showMoreBtn.isVisible();
            if (!isVisible) {
                break;
            }

            // Check hidden class
            const isHidden = await page.evaluate(() => {
                const container = document.getElementById('showMoreBtnContainer');
                return container ? container.classList.contains('hidden') : true;
            });
            if (isHidden) {
                break;
            }

            clickCount++;
            console.log(`[Scraper] Clicking 'Afficher plus' (Click #${clickCount})...`);

            const prevProductCount = await page.locator('[data-asin]').count();

            // Click the button
            await showMoreBtn.click({ timeout: 5000 });

            // Wait for the number of products to increase
            try {
                await page.waitForFunction(
                    (prevCount) => document.querySelectorAll('[data-asin]').length > prevCount,
                    prevProductCount,
                    { timeout: 8000 }
                );
                await page.waitForTimeout(500);
            } catch (e) {
                console.log("[Scraper] No new products loaded after click, stopping click loop.");
                break;
            }
        }

        console.log(`[Scraper] Click loop finished. Total clicks: ${clickCount}`);

        // Extract product details from DOM
        const products = await page.evaluate(() => {
            const items = Array.from(document.querySelectorAll('[data-asin]'));
            return items.map(el => {
                const asin = el.getAttribute('data-asin');
                const merchantId = el.getAttribute('data-merchant') || '';

                const titleEl = el.querySelector('[data-name="productTitle"]');
                const title = titleEl ? titleEl.textContent.trim() : 'Sans titre';
                let productUrl = titleEl ? titleEl.getAttribute('href') : '';
                if (productUrl && productUrl.startsWith('/')) {
                    productUrl = 'https://www.amazon.fr' + productUrl;
                }

                const imgEl = el.querySelector('img[name="productImage"]');
                let imageUrl = imgEl ? imgEl.getAttribute('src') : '';
                if (imageUrl) {
                    imageUrl = imageUrl.replace(/\._[A-Z0-9_-]+\.(jpg|jpeg|gif|png|webp)$/i, '.$1');
                }

                const pricePayEl = el.querySelector('div[name="productPriceBox"] [name="productPriceToPay"]');
                let price = '';
                if (pricePayEl) {
                    price = pricePayEl.getAttribute('aria-label');
                }
                if (!price) {
                    const priceBoxEl = el.querySelector('div[name="productPriceBox"]');
                    price = priceBoxEl ? priceBoxEl.textContent.trim() : '';
                }

                // Exclude any script/style elements inside availability
                const availEl = el.querySelector('[data-name="availabilityMessage"]');
                let availabilityText = '';
                if (availEl) {
                    const clone = availEl.cloneNode(true);
                    clone.querySelectorAll('script, style').forEach(s => s.remove());
                    availabilityText = clone.textContent.trim();
                }

                // Exclude any script/style elements inside delivery
                const delivEl = el.querySelector('[name="productDeliveryBox"]');
                let deliveryText = '';
                if (delivEl) {
                    const clone = delivEl.cloneNode(true);
                    clone.querySelectorAll('script, style').forEach(s => s.remove());
                    deliveryText = clone.textContent.trim().replace(/\s+/g, ' ');
                }

                return {
                    asin,
                    merchantId,
                    title,
                    url: productUrl || `https://www.amazon.fr/dp/${asin}`,
                    imageUrl,
                    price,
                    availabilityText,
                    deliveryText,
                    isAvailable: false
                };
            }).filter(item => item.asin && item.asin.trim().length > 0);
        });

        console.log(`[Scraper] Successfully extracted ${products.length} products.`);
        return products;

    } catch (error) {
        console.error("[Scraper] Scrape error:", error);
        await closePersistentBrowser();
        throw error;
    } finally {
        await page.close().catch(() => {});
    }
}

/**
 * Scrapes direct product pages for a list of ASINs in parallel,
 * forcing the first-party Amazon merchant ID to bypass headless guest delivery box limitations.
 * Uses aggressive resource blocking to optimize performance.
 * @param {Array<string>} asins - List of ASINs to scrape
 * @param {boolean} headless - Headless flag
 * @returns {Promise<Array<Object>>} Extracted delivery and availability info per ASIN
 */
async function scrapeProductPages(asins, headless = true) {
    if (!asins || asins.length === 0) return [];

    // Ensure persistent context is initialized
    const context = await getPersistentContext(headless);

    console.log(`[Product Scraper] Scraping ${asins.length} product pages using persistent browser session...`);

    const promises = asins.map(async (asin) => {
        let page = null;
        try {
            page = await context.newPage();

            // Optimize speed and bandwidth by blocking heavy resources
            await page.route('**/*', (route) => {
                const req = route.request();
                const type = req.resourceType();
                const reqUrl = req.url();

                if (
                    type === 'image' ||
                    type === 'stylesheet' ||
                    type === 'font' ||
                    type === 'media' ||
                    reqUrl.includes('google-analytics') ||
                    reqUrl.includes('analytics') ||
                    reqUrl.includes('amazon-adsystem') ||
                    reqUrl.includes('doubleclick') ||
                    reqUrl.includes('device-metrics')
                ) {
                    route.abort();
                } else {
                    route.continue();
                }
            });

            const url = `https://www.amazon.fr/dp/${asin}?m=A1X6FK5RDHNB96`;
            await page.goto(url, { waitUntil: 'networkidle', timeout: timeoutMs });

            // Wait briefly for delivery block or availability to appear
            try {
                await page.waitForSelector('#mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE, #mir-layout-DELIVERY_BLOCK-slot-NO_PROMISE_UPSELL_MESSAGE, #availability', { timeout: 6000 });
            } catch (e) {
                // Settle for parsing what's loaded
            }

            const result = await page.evaluate((asin) => {
                const titleEl = document.getElementById('productTitle');
                const title = titleEl ? titleEl.textContent.trim() : '';

                // Exclude any script/style elements inside availability
                const availEl = document.getElementById('availability');
                let availabilityText = '';
                if (availEl) {
                    const clone = availEl.cloneNode(true);
                    clone.querySelectorAll('script, style').forEach(s => s.remove());
                    availabilityText = clone.textContent.trim();
                }

                // Exclude any script/style elements inside delivery (try primary first, then fallback first-order upsell)
                const delivEl = document.getElementById('mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE') ||
                    document.getElementById('mir-layout-DELIVERY_BLOCK-slot-NO_PROMISE_UPSELL_MESSAGE');
                let deliveryText = '';
                if (delivEl) {
                    const clone = delivEl.cloneNode(true);
                    clone.querySelectorAll('script, style').forEach(s => s.remove());
                    deliveryText = clone.textContent.trim().replace(/\s+/g, ' ');
                }

                const imgEl = document.getElementById('landingImage') || document.getElementById('imgBlkFront');
                let imageUrl = imgEl ? imgEl.getAttribute('src') : '';
                if (imageUrl) {
                    imageUrl = imageUrl.replace(/\._[A-Z0-9_-]+\.(jpg|jpeg|gif|png|webp)$/i, '.$1');
                }

                return {
                    asin,
                    title,
                    availabilityText,
                    deliveryText,
                    imageUrl
                };
            }, asin);

            return result;
        } catch (err) {
            console.error(`[Product Scraper] Error on ASIN ${asin}:`, err.message);
            return {
                asin,
                availabilityText: '',
                deliveryText: '',
                error: err.message
            };
        } finally {
            if (page) {
                await page.close().catch(() => {});
            }
        }
    });

    return Promise.all(promises);
}

module.exports = {
    scrapePromoPage,
    scrapeProductPages,
    determineAvailability,
    closePersistentBrowser
};
