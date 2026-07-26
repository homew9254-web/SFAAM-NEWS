/* ============================================
   SFAAM NEWS V19 - Global Configuration
   Premium News Platform with TL;DR + Fact-Check + Audio
   ============================================ */
const SFAAM_CONFIG = {
  siteName: 'SFAAM NEWS',
  siteTagline: 'Trusted Journalism. Global Perspective.',
  siteUrl: 'https://sfaamnews.com',
  foundedYear: 2024,
  version: '19.0',
  updateInterval: 'hourly',

  editorialTeam: [
    { name: 'Shamsulhaq', role: 'Founder & CEO', expertise: 'Editorial Direction, Strategy', bio: 'Founder of SFAAM NEWS. Leads editorial direction, AI pipeline design, and long-term strategy from Bagrot, Gilgit-Baltistan.' },
    { name: 'James Mitchell', role: 'World Desk Editor', expertise: 'International Affairs', bio: 'Oversees the World desk. 15+ years covering global politics, diplomacy, and breaking international news.' },
    { name: 'Emily Watson', role: 'USA Desk Editor', expertise: 'US Politics & Policy', bio: 'Covers Washington politics, elections, and federal policy. Background in investigative reporting.' },
    { name: 'Oliver Brown', role: 'UK Desk Editor', expertise: 'British Politics', bio: 'London-based editor covering Westminster, the monarchy, and UK economic affairs.' },
    { name: 'Ahmed Khan', role: 'Pakistan Desk Editor', expertise: 'South Asia & Pakistan', bio: 'Veteran of Pakistan affairs. Covers Islamabad politics, security, and Pakistan-India relations.' },
    { name: 'Priya Sharma', role: 'India Desk Editor', expertise: 'India & South Asia', bio: 'New Delhi-based editor covering Indian politics, economy, and regional diplomacy.' },
    { name: 'Klaus Weber', role: 'Germany Desk Editor', expertise: 'European & German Affairs', bio: 'Berlin-based editor covering German politics, EU affairs, and the European economy.' }
  ],

  founder: {
    name: 'Shamsulhaq',
    title: 'Founder & CEO, SFAAM NEWS',
    from: 'Bagrot, Gilgit-Baltistan',
    age: 18,
    bio: 'Shamsulhaq is the founder and CEO of SFAAM NEWS, an independent digital news platform built to bring fast, reliable, and accessible journalism to readers around the world. Originally from Bagrot in Gilgit-Baltistan, he started SFAAM NEWS in 2024 with the goal of making global news easier to follow for everyone, everywhere.'
  },

  social: {
    facebook: 'https://facebook.com/sfaamnews',
    twitter: 'https://twitter.com/sfaamnews',
    instagram: 'https://instagram.com/sfaamnews',
    youtube: 'https://youtube.com/@sfaamnews',
    tiktok: 'https://tiktok.com/@sfaamnews',
    telegram: 'https://t.me/sfaamnews',
    whatsapp: 'https://wa.me/923431188853',
    email: 'mailto:editorial@sfaamnews.com',
  },

  contact: {
    editorial: 'editorial@sfaamnews.com',
    tips: 'tips@sfaamnews.com',
    advertising: 'ads@sfaamnews.com',
    phone: '+92 343 1188853',
    address: 'SFAAM Media House, Bagrot, Gilgit-Baltistan, Pakistan'
  },

  analytics: { enabled: true, domain: 'sfaamnews.com', src: 'https://plausible.io/js/script.js', gaId: '' },
  images: { logo: 'logo.png', placeholder: 'images/placeholder.jpg', founder: 'images/founder.png', founderNav: 'images/founder-nav.png' },

  features: {
    darkMode: true,
    pushNotifications: true,
    offlineReading: true,
    readingTime: true,
    bookmarks: true,
    fontSize: true,
    ticker: true,
    aiSummary: true,
    progressBar: true,
    shareButtons: true,
    newsletter: true
  },

  // Region configuration
  regions: {
    world: { label: 'World', flag: '\uD83C\uDF0D', priority: 1 },
    usa: { label: 'USA', flag: '\uD83C\uDDFA\uD83C\uDDF8', priority: 2 },
    uk: { label: 'UK', flag: '\uD83C\uDDEC\uD83C\uDDE7', priority: 3 },
    pakistan: { label: 'Pakistan', flag: '\uD83C\uDDF5\uD83C\uDDF0', priority: 4 },
    india: { label: 'India', flag: '\uD83C\uDDEE\uD83C\uDDF3', priority: 5 },
    germany: { label: 'Germany', flag: '\uD83C\uDDE9\uD83C\uDDEA', priority: 6 }
  }
};

if (typeof module !== 'undefined' && module.exports) module.exports = SFAAM_CONFIG;
