--[[ Server monitor for KOReader. Shows the PNG served by eink_monitor.py and refetches it
every 30 s (the server sets the interval). Tap or a page key toggles the frontlight, swipe closes.
Open it from KOReader's Tools menu > Server monitor.
Install: copy this folder into the plugins folder of KOReader on the reader. ]]
local Blitbuffer = require("ffi/blitbuffer")
local CenterContainer = require("ui/widget/container/centercontainer")
local Device = require("device")
local Font = require("ui/font")
local Geom = require("ui/geometry")
local GestureRange = require("ui/gesturerange")
local ImageWidget = require("ui/widget/imagewidget")
local InputContainer = require("ui/widget/container/inputcontainer")
local NetworkMgr = require("ui/network/manager")
local PluginShare = require("pluginshare")
local TextBoxWidget = require("ui/widget/textboxwidget")
local UIManager = require("ui/uimanager")
local WidgetContainer = require("ui/widget/container/widgetcontainer")
local http = require("socket.http")
local ltn12 = require("ltn12")
local socketutil = require("socketutil")
local _ = require("gettext")
local Screen = Device.screen

local BASE = "http://SERVER_ADDRESS:8605"   -- EDIT: address and port of the machine running eink_monitor.py
local EVERY = 30    -- seconds between frames until the server says otherwise (its X-Refresh header wins)
local RETRY = 10    -- seconds until the next try after a failed fetch (KOReader reports Wi-Fi "connected" before DHCP is done)
local FLASH = 10    -- full e-ink flash every Nth frame to clear ghosting
local FILE = "/tmp/sysmon.png"

local function get(path, file)   -- returns true + response headers, or false + the reason
    local chunks = {}
    socketutil:set_timeout(8, 15)
    local ok, code, headers = http.request{ url = BASE .. path, sink = ltn12.sink.table(chunks) }
    socketutil:reset_timeout()
    if not ok or code ~= 200 then
        return false, tostring(code)   -- on a socket error `code` is the reason, e.g. "connection refused"
    end
    local f, err = io.open(file, "wb")
    if not f then
        return false, err
    end
    f:write(table.concat(chunks))
    f:close()
    return true, headers
end

local View = InputContainer:extend{ covers_fullscreen = true }

function View:init()
    self.dimen = Geom:new{ x = 0, y = 0, w = Screen:getWidth(), h = Screen:getHeight() }
    self.ges_events = {
        Tap = { GestureRange:new{ ges = "tap", range = self.dimen } },
        Swipe = { GestureRange:new{ ges = "swipe", range = self.dimen } },
    }
    if Device:hasKeys() then
        self.key_events = {
            Tap = { { Device.input.group.PgFwd } },
            TapBack = { { Device.input.group.PgBack }, event = "Tap" },
        }
    end
    self.frames, self.every = 0, EVERY
    self.tick = function()
        local ok = self:load()
        self.frames = self.frames + 1
        UIManager:setDirty(self, self.frames % FLASH == 0 and "full" or "ui")
        UIManager:scheduleIn(ok and self.every or RETRY, self.tick)
    end
    -- Always on: no auto-suspend, no standby, until closed.
    PluginShare.pause_auto_suspend = true
    UIManager:preventStandby()
    UIManager:scheduleIn(self:load() and self.every or RETRY, self.tick)
end

function View:load()   -- returns true when a fresh frame is up
    -- every= is printed in the frame's corner ("refreshes every 30 sec"), so it must be the interval we really use
    -- bat= is this reader's own battery %, drawn in the frame's corner next to the refresh interval
    local bat = Device:hasBattery() and Device:getPowerDevice():getCapacity() or nil
    local ok, res = get(string.format("/frame.png?w=%d&h=%d&every=%d%s", self.dimen.w, self.dimen.h, self.every,
        bat and "&bat=" .. bat or ""), FILE)
    if self[1] then
        self[1]:free()
    end
    if ok then
        local n = tonumber(res and res["x-refresh"])
        if n and n >= 5 and n <= 86400 then
            self.every = n
        end
        self.last_ok = (os.date("%I:%M %p"):gsub("^0", ""))   -- 12-hour, always
        self[1] = ImageWidget:new{ file = FILE, file_do_cache = false, width = self.dimen.w, height = self.dimen.h }
    else   -- never leave a stale frame up as if it were live
        self[1] = CenterContainer:new{
            dimen = self.dimen:copy(),
            TextBoxWidget:new{
                text = string.format("NO LINK TO SERVER\n\n%s\n%s\n\nlast good frame: %s\nretrying every %d sec\n\ntap = light   swipe = exit",
                    BASE, res, self.last_ok or "none yet", RETRY),
                face = Font:getFace("infofont", 24),
                width = math.floor(self.dimen.w * 0.85),
                alignment = "center",
            },
        }
    end
    return ok
end

function View:paintTo(bb, x, y)
    bb:paintRect(x, y, self.dimen.w, self.dimen.h, Blitbuffer.COLOR_WHITE)   -- backdrop for the NO LINK text
    InputContainer.paintTo(self, bb, x, y)
end

function View:onTap()
    Device:getPowerDevice():toggleFrontlight()
    return true
end

function View:onSwipe()
    UIManager:close(self, "full")
    if self.quit_ui then   -- started with SYSMON_AUTOSTART=1 in the environment: leave KOReader too when closed
        local ui = self.quit_ui
        UIManager:nextTick(function() ui:onClose() end)
    end
    return true
end

function View:onCloseWidget()
    UIManager:unschedule(self.tick)
    UIManager:allowStandby()
    PluginShare.pause_auto_suspend = false
    if self[1] then
        self[1]:free()
    end
end

local SysMon = WidgetContainer:extend{ name = "sysmon", is_doc_only = false }

local function open(quit_ui)
    NetworkMgr:runWhenConnected(function()
        UIManager:show(View:new{ quit_ui = quit_ui }, "full")
    end)
end

function SysMon:init()
    self.ui.menu:registerToMainMenu(self)
    -- Optional: launch KOReader with SYSMON_AUTOSTART=1 to open the monitor at once; swipe then exits KOReader as well.
    if os.getenv("SYSMON_AUTOSTART") and not PluginShare.sysmon_started then
        PluginShare.sysmon_started = true
        UIManager:nextTick(function() open(self.ui) end)
    end
end

function SysMon:addToMainMenu(menu_items)
    menu_items.sysmon = {
        text = _("Server monitor"),
        sorting_hint = "tools",
        callback = function() open() end,
    }
end

return SysMon
