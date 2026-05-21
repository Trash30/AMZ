const dotenv = require('dotenv');
dotenv.config();

const webhookUrl = process.env.DISCORD_WEBHOOK_URL;

/**
 * Sends a stylized embed message to Discord via webhook.
 * @param {Object} params
 * @param {'new'|'restock'} params.type - The type of event (new product or back in stock)
 * @param {Object} params.product - The product details
 * @param {string} params.product.asin - ASIN
 * @param {string} params.product.title - Product title
 * @param {string} params.product.url - Direct Amazon URL
 * @param {string} params.product.imageUrl - Image URL
 * @param {string} params.product.price - Price string
 * @param {string} params.product.deliveryText - Delivery information
 * @param {string} params.product.availabilityText - Availability message
 * @param {string} [params.previousAvailability] - Previous stock/availability message (if restock)
 * @param {string} [params.previousDelivery] - Previous delivery date/block (if restock)
 */
async function sendDiscordNotification({ type, product, previousAvailability = '', previousDelivery = '' }) {
    if (!webhookUrl) {
        console.error("[Discord] Error: DISCORD_WEBHOOK_URL is not set in the environment.");
        return;
    }

    const isNew = type === 'new';
    
    // Embed colors (integers)
    // Blue for new products: #3498db (3447003)
    // Green for restocks: #2ecc71 (3066993)
    const color = isNew ? 3447003 : 3066993;
    const emoji = isNew ? '🆕' : '🟢';
    const statusText = isNew ? 'Nouveau produit détecté !' : 'Produit de retour en stock !';
    
    // Helper to sanitize strings and ensure they comply with Discord embed rules (not empty, no raw scripting, safe length)
    const sanitize = (text, maxLength = 800) => {
        if (!text || typeof text !== 'string' || text.trim().length === 0) return 'Non spécifié';
        let clean = text.trim().replace(/\s+/g, ' ');
        if (clean.length > maxLength) {
            clean = clean.substring(0, maxLength - 3) + '...';
        }
        return clean;
    };

    // Helper to clean Amazon delivery boilerplate for cleaner Discord alerts
    const cleanDeliveryForDiscord = (text) => {
        if (!text || typeof text !== 'string') return text;
        return text
            .replace(/GRATUITE/ig, '')
            .replace(/lors de votre première commande/ig, '')
            .replace(/pour votre première commande/ig, '')
            .replace(/en France métropolitaine, Belgique et Luxembourg\.?/ig, '')
            .replace(/en France métropolitaine/ig, '')
            .replace(/pour les membres Prime/ig, '')
            .replace(/\s+/g, ' ')
            .trim();
    };

    // Clean strings and add defaults
    const priceText = sanitize(product.price);
    const availabilityText = sanitize(product.availabilityText);
    const deliveryText = sanitize(cleanDeliveryForDiscord(product.deliveryText));
    
    const fields = [
        {
            name: '💵 Prix',
            value: `**${priceText}**`,
            inline: true
        },
        {
            name: '🆔 ASIN',
            value: `\`${product.asin}\``,
            inline: true
        }
    ];

    if (isNew) {
        fields.push(
            {
                name: '📢 Statut',
                value: availabilityText,
                inline: false
            },
            {
                name: '🚚 Livraison',
                value: deliveryText,
                inline: false
            }
        );
    } else {
        // For restocks, show transition details
        fields.push(
            {
                name: '🚚 Infos de Livraison',
                value: `🔴 Avant : Pas dispo\n🟢 Maintenant : ${deliveryText}`,
                inline: false
            }
        );
    }

    const associateTag = process.env.AMAZON_ASSOCIATE_TAG || 'botbluray-21';

    const embed = {
        title: `${emoji} ${statusText}`,
        description: `**[${product.title}](${product.url})**\n\n🛒 **[Ajouter 2 ex. au panier (ATC)](https://www.amazon.fr/gp/aws/cart/add.html?ASIN.1=${product.asin}&Quantity.1=2&AssociateTag=${associateTag})**`,
        url: product.url,
        color: color,
        fields: fields,
        footer: {
            text: 'Bot Amazon Promotion Bluray'
        },
        timestamp: new Date().toISOString()
    };

    if (product.imageUrl && product.imageUrl.startsWith('http')) {
        embed.image = {
            url: product.imageUrl
        };
    }

    const payload = {
        embeds: [embed]
    };

    try {
        const response = await fetch(webhookUrl, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(payload)
        });

        if (!response.ok) {
            const errorText = await response.text();
            console.error(`[Discord] Failed to send webhook notification. Status: ${response.status}. Error: ${errorText}`);
        } else {
            console.log(`[Discord] Alert sent successfully for ASIN ${product.asin} (${type})`);
        }
    } catch (e) {
        console.error(`[Discord] Network error sending alert to webhook:`, e);
    }
}

module.exports = {
    sendDiscordNotification
};
