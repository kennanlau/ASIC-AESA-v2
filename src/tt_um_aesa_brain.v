`default_nettype none
// Pins
//  ui[0] wr (rising edge)   ui[1] start_single (rising edge)   ui[4:2] sel
//  ui[5] start_burst (rising edge)   ui[6] SDO from ADAR1000   ui[7] read-out enable
//  uo[0] SCLK  uo[1] SDIO/MOSI  uo[2] CSB  uo[3] done (sticky)  uo[4] busy
//  uo[5] RX_LOAD  uo[6] TX_LOAD
//  uio[7:0] data in; when ui[7]=1 they drive out the last read byte
module tt_um_aesa_brain (
    input  wire [7:0] ui_in,
    output wire [7:0] uo_out,
    input  wire [7:0] uio_in,
    output wire [7:0] uio_out,
    output wire [7:0] uio_oe,
    input  wire       ena,
    input  wire       clk,
    input  wire       rst_n
);
    // two-flop synchronizers plus one more stage for edge detection
    reg [7:0] s1, s2, s3;
    always @(posedge clk) begin
        if (!rst_n) begin s1 <= 8'd0; s2 <= 8'd0; s3 <= 8'd0; end
        else begin s1 <= ui_in; s2 <= s1; s3 <= s2; end
    end
    wire wr           = s2[0] & ~s3[0];
    wire start_single = s2[1] & ~s3[1];
    wire start_burst  = s2[5] & ~s3[5];
    wire rd_oe        = s2[7];

    wire sclk, mosi, cs_n, busy, done_sticky, rx_load, tx_load;
    wire [7:0] rx_byte;

    aesa_brain brain (
        .clk(clk), .rst(!rst_n), .wr(wr), .start_single(start_single), .start_burst(start_burst),
        .sel(s2[4:2]), .din(uio_in), .miso(s2[6]),
        .sclk(sclk), .mosi(mosi), .cs_n(cs_n), .busy(busy), .done_sticky(done_sticky),
        .rx_load(rx_load), .tx_load(tx_load), .rx_byte(rx_byte)
    );

    assign uo_out  = {1'b0, tx_load, rx_load, busy, done_sticky, cs_n, mosi, sclk};
    assign uio_out = rx_byte;
    assign uio_oe  = {8{rd_oe}};
    wire _unused = &{ena, s3[7:6], s3[4:2], 1'b0};
endmodule
